"""Adapter that turns the existing AgentLoop into a governed WorkerRunner."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from minicode.runtime_artifacts import RuntimeArtifactStore
from minicode.runtime_protocol import (
    AgentInvocationSpec,
    AgentResult,
    AgentResultStatus,
    ArtifactRef,
    ContextManifest,
    EvidenceRef,
    ExecutionFailure,
    ExecutionFailureKind,
    ReplayPolicy,
    ToolEffect,
)
from minicode.tooling import (
    ToolCapability,
    ToolDefinition,
    ToolMetadata,
    ToolRegistry,
    ToolResult,
)
from minicode.turn_events import TurnEvent
from minicode.workspace_provider import (
    DirectWorkspaceProvider,
    TemporaryWorkspaceProvider,
    WorkspaceConflict,
    WorkspaceHandle,
    workspace_snapshot,
)

INLINE_RESULT_LIMIT = 8_000
SUMMARY_LIMIT = 2_000


class _IntermediateWorkerEventSink:
    """Forward Worker activity while reserving final delivery for the coordinator."""

    def __init__(self, downstream: Any) -> None:
        self.downstream = downstream
        self.include_stream_chunks = bool(
            getattr(downstream, "include_stream_chunks", False)
        )
        self.include_thinking_chunks = bool(
            getattr(downstream, "include_thinking_chunks", False)
        )

    def on_event(self, event: TurnEvent) -> None:
        if event.kind in {"assistant", "assistant_stream", "done"}:
            return
        on_event = getattr(self.downstream, "on_event", None)
        if callable(on_event):
            on_event(event)
            return
        callback_name = {
            "tool_start": "on_tool_start",
            "tool_result": "on_tool_result",
            "progress": "on_progress_message",
            "runtime": "on_runtime_event",
            "thinking": "on_thinking_chunk",
        }.get(event.kind)
        handler = getattr(self.downstream, callback_name, None) if callback_name else None
        if not callable(handler):
            return
        if event.kind == "tool_start":
            handler(event.tool_name, event.tool_input)
        elif event.kind == "tool_result":
            handler(event.tool_name, event.output, event.is_error)
        elif event.kind == "runtime" and event.runtime_event is not None:
            handler(event.runtime_event)
        else:
            handler(event.content)


def snapshot_revision(snapshot: dict[str, str]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def workspace_revision(workspace: Path) -> str:
    return snapshot_revision(workspace_snapshot(workspace))


class AgentLoopWorkerRunner:
    """Execute one registered Node without giving AgentLoop global state access."""

    def __init__(
        self,
        *,
        model: Any,
        tools: ToolRegistry,
        messages: list[dict[str, Any]],
        cwd: str,
        runtime: dict[str, Any] | None = None,
        permissions: Any | None = None,
        session: Any | None = None,
        artifact_store: RuntimeArtifactStore | None = None,
        cancellation_event: threading.Event | None = None,
        model_factory: Callable[[ToolRegistry], Any] | None = None,
        runtime_context: Any | None = None,
        isolate_writes: bool = False,
        temporary_root: Path | None = None,
        agent_turn_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.base_messages = list(messages)
        self.cwd = Path(cwd).resolve()
        self.runtime = runtime if runtime is not None else {}
        # Keep the lossless memory authority outside isolated Writer clones.
        # PagedMemoryConfig uses this stable coordinator root when resolving
        # relative storage settings inherited by each Worker.
        self.runtime.setdefault("_memoryAuthorityCwd", str(self.cwd))
        self.permissions = permissions
        self.session = session
        self.artifact_store = artifact_store or RuntimeArtifactStore()
        self.cancellation_event = cancellation_event or threading.Event()
        self.model_factory = model_factory
        self.runtime_context = runtime_context
        self.isolate_writes = isolate_writes
        self.temporary_provider = TemporaryWorkspaceProvider(temporary_root)
        self.direct_provider = DirectWorkspaceProvider()
        self.agent_turn_kwargs = dict(agent_turn_kwargs or {})
        self._results: dict[str, AgentResult] = {}
        self._messages: dict[str, list[dict[str, Any]]] = {}
        self._exceptions: dict[str, Exception] = {}
        self._manifests: dict[str, ContextManifest] = {}
        self._pending_workspaces: dict[
            tuple[str, str, str], tuple[Any, WorkspaceHandle]
        ] = {}
        self._cleanup_backlog: dict[
            tuple[str, str, str], tuple[Any, WorkspaceHandle]
        ] = {}
        self._cleanup_errors: dict[tuple[str, str, str], Exception] = {}
        self._lock = threading.Lock()

    def result_for(self, node_id: str) -> AgentResult | None:
        with self._lock:
            return self._results.get(node_id)

    def messages_for(self, node_id: str) -> list[dict[str, Any]] | None:
        with self._lock:
            messages = self._messages.get(node_id)
            return list(messages) if messages is not None else None

    def exception_for(self, node_id: str) -> Exception | None:
        with self._lock:
            return self._exceptions.get(node_id)

    def context_manifests(self) -> tuple[ContextManifest, ...]:
        with self._lock:
            return tuple(self._manifests.values())

    @staticmethod
    def _result_key(result: AgentResult) -> tuple[str, str, str]:
        return result.run_id, result.node_id, result.attempt_id

    def commit_result(self, result: AgentResult) -> tuple[str, ...]:
        """Commit a prepared isolated Writer after Reducer acceptance."""
        key = self._result_key(result)
        with self._lock:
            pending = self._pending_workspaces.pop(key, None)
        if pending is None:
            # Direct Standard Workers have already written in their sole Writer
            # slot; read-only Workers never own a prepared workspace.
            return result.changed_resources
        provider, handle = pending
        try:
            prepared = handle.changed_resources()
            if prepared != result.changed_resources:
                raise WorkspaceConflict("prepared workspace no longer matches AgentResult")
            return provider.integrate(handle)
        finally:
            self._safe_cleanup(key, provider, handle)

    def discard_result(self, result: AgentResult) -> None:
        """Discard a rejected, canceled, or failed prepared workspace."""
        key = self._result_key(result)
        with self._lock:
            pending = self._pending_workspaces.pop(key, None)
        if pending is not None:
            provider, handle = pending
            self._safe_cleanup(key, provider, handle)

    def retry_cleanup(self) -> tuple[Exception, ...]:
        """Retry transient cleanup failures without changing execution outcome."""
        with self._lock:
            backlog = tuple(self._cleanup_backlog.items())
            self._cleanup_backlog.clear()
        failures: list[Exception] = []
        for key, (provider, handle) in backlog:
            try:
                provider.cleanup(handle)
            except Exception as error:  # noqa: BLE001 - cleanup adapters vary.
                failures.append(error)
                with self._lock:
                    self._cleanup_backlog[key] = (provider, handle)
                    self._cleanup_errors[key] = error
        return tuple(failures)

    def cleanup_error_for(self, result: AgentResult) -> Exception | None:
        with self._lock:
            return self._cleanup_errors.get(self._result_key(result))

    def _safe_cleanup(
        self,
        key: tuple[str, str, str],
        provider: Any,
        handle: WorkspaceHandle,
    ) -> None:
        try:
            provider.cleanup(handle)
        except Exception as error:  # noqa: BLE001 - cleanup adapters vary.
            with self._lock:
                self._cleanup_backlog[key] = (provider, handle)
                self._cleanup_errors[key] = error

    def run(self, invocation: AgentInvocationSpec) -> AgentResult:
        with self._lock:
            self._manifests[invocation.node_id] = invocation.context_manifest
        if self.cancellation_event.is_set():
            return self._canceled(invocation, "parent run canceled before Worker start")

        is_writer = ToolEffect.WORKSPACE_WRITE in invocation.allowed_effects
        standard_fast_path = invocation.role == "standard"
        provider = self.temporary_provider if is_writer and self.isolate_writes else self.direct_provider
        handle: WorkspaceHandle | None = None
        retain_handle = False
        before_source = None if standard_fast_path else workspace_snapshot(self.cwd)
        checkpoint_offset = len(getattr(self.session, "checkpoints", []) or [])
        pending_replay_tools: dict[str, list[ReplayPolicy]] = {}
        try:
            if standard_fast_path:
                handle = WorkspaceHandle(
                    source=self.cwd,
                    workspace=self.cwd,
                    base_hashes={},
                    isolated=False,
                    owner_token=invocation.attempt_id,
                )
            else:
                handle = provider.create(self.cwd, owner_token=invocation.attempt_id)
            worker_tools = self.tools.filtered_by_effects(set(invocation.allowed_effects))
            if ToolEffect.READ_ONLY in invocation.allowed_effects:
                artifact_tools = self._dependency_artifact_tools(invocation)
                if artifact_tools:
                    worker_tools = worker_tools.with_additional_tools(artifact_tools)
            worker_model = self._select_model(worker_tools, invocation)
            worker_permissions = self._permissions_for(handle.workspace)
            worker_messages = self._messages_for_invocation(invocation)
            worker_runtime = self.runtime if invocation.role == "standard" else dict(self.runtime)
            worker_runtime.update(
                {
                    "_runtimeContext": self.runtime_context,
                    "_allowedToolEffects": [effect.value for effect in invocation.allowed_effects],
                    "_cancellationEvent": self.cancellation_event,
                    "_runtimeNodeId": invocation.node_id,
                    "_runtimeAttemptId": invocation.attempt_id,
                    "_runtimeIdempotencyKey": invocation.idempotency_key,
                    "_runtimeActiveRunId": invocation.run_id,
                    "_runtimeBaseRevision": invocation.context_manifest.base_revision,
                    "runtimeProfile": (
                        "single-deep"
                        if invocation.role in {"deep", "writer", "verifier"}
                        else self.runtime.get("runtimeProfile", "single")
                    ),
                }
            )

            from minicode.agent_loop import run_agent_turn

            call_kwargs = dict(self.agent_turn_kwargs)
            if not standard_fast_path:
                callbacks = call_kwargs.get("callbacks")
                if callbacks is not None:
                    call_kwargs["callbacks"] = _IntermediateWorkerEventSink(callbacks)
                # Legacy final callbacks have no event kind to distinguish a
                # Worker answer from the coordinator's accepted delivery.
                call_kwargs["on_assistant_message"] = None
                call_kwargs["on_assistant_stream_chunk"] = None
            prior_tool_start = call_kwargs.get("on_tool_start")
            prior_tool_result = call_kwargs.get("on_tool_result")

            def governed_tool_start(tool_name: str, tool_input: dict) -> None:
                definition = worker_tools.find(tool_name)
                if definition is not None:
                    pending_replay_tools.setdefault(tool_name, []).append(definition.replay_policy)
                if callable(prior_tool_start):
                    prior_tool_start(tool_name, tool_input)

            def governed_tool_result(tool_name: str, output: str, is_error: bool) -> None:
                pending = pending_replay_tools.get(tool_name)
                if pending:
                    pending.pop(0)
                    if not pending:
                        pending_replay_tools.pop(tool_name, None)
                if callable(prior_tool_result):
                    prior_tool_result(tool_name, output, is_error)

            call_kwargs.update(
                {
                    "model": worker_model,
                    "tools": worker_tools,
                    "messages": worker_messages,
                    "cwd": str(handle.workspace),
                    "permissions": worker_permissions,
                    "session": self.session,
                    "runtime": worker_runtime,
                    "on_tool_start": governed_tool_start,
                    "on_tool_result": governed_tool_result,
                }
            )
            result_messages = run_agent_turn(**call_kwargs)
            output = self._final_output(result_messages)
            if invocation.role == "verifier" and "<verification_passed>" not in output.lower():
                reason = (
                    output
                    if "<verification_failed>" in output.lower()
                    else "Verifier did not emit the required <verification_passed> decision marker"
                )
                return self._failed(
                    invocation,
                    AgentResultStatus.FAILED_TERMINAL,
                    ExecutionFailureKind.VERIFICATION_FAILED,
                    reason,
                )
            changed = () if standard_fast_path else handle.changed_resources()

            if not is_writer and changed:
                return self._failed(
                    invocation,
                    AgentResultStatus.FAILED_TERMINAL,
                    ExecutionFailureKind.POLICY_DENIED,
                    "read-only Worker modified its workspace: " + ", ".join(changed),
                )
            if self.cancellation_event.is_set():
                return self._canceled(invocation, "parent run canceled before workspace integration")

            if standard_fast_path and self.session is not None:
                changed = tuple(
                    sorted(
                        {
                            str(getattr(item, "file_path", ""))
                            for item in (getattr(self.session, "checkpoints", []) or [])[checkpoint_offset:]
                            if str(getattr(item, "file_path", ""))
                        }
                    )
                )
            after_source = None if standard_fast_path else workspace_snapshot(self.cwd)
            if not is_writer and before_source != after_source:
                return self._failed(
                    invocation,
                    AgentResultStatus.FAILED_TERMINAL,
                    ExecutionFailureKind.POLICY_DENIED,
                    "read-only Worker changed the integrated workspace",
                )

            usage = self._usage_for(result_messages, worker_messages)
            result = self._structured_result(invocation, output, tuple(changed), usage)
            with self._lock:
                self._results[invocation.node_id] = result
                self._messages[invocation.node_id] = [dict(message) for message in result_messages]
                if is_writer and not standard_fast_path:
                    self._pending_workspaces[self._result_key(result)] = (provider, handle)
                    retain_handle = True
            return result
        except WorkspaceConflict as error:
            return self._failed(
                invocation,
                AgentResultStatus.FAILED_TERMINAL,
                ExecutionFailureKind.VERIFICATION_FAILED,
                str(error),
            )
        except Exception as error:  # noqa: BLE001 - worker failures become results.
            with self._lock:
                self._exceptions[invocation.node_id] = error
            unsafe_replay_tools = {
                name
                for name, policies in pending_replay_tools.items()
                if any(
                    policy in {ReplayPolicy.NEVER_AUTO_RETRY, ReplayPolicy.VERIFY_BEFORE_RETRY}
                    for policy in policies
                )
            }
            if unsafe_replay_tools:
                return self._failed(
                    invocation,
                    AgentResultStatus.FAILED_TERMINAL,
                    ExecutionFailureKind.TOOL_FAILED,
                    (
                        f"Worker failed with an ambiguous non-safe tool call "
                        f"({', '.join(sorted(unsafe_replay_tools))}); "
                        "automatic replay is forbidden"
                    ),
                    retryable=False,
                    ambiguous_side_effect=True,
                )
            return self._failed(
                invocation,
                AgentResultStatus.FAILED_RETRYABLE,
                ExecutionFailureKind.TOOL_FAILED,
                f"Worker failed ({type(error).__name__}): {error}",
                retryable=True,
            )
        finally:
            if handle is not None and not retain_handle:
                key = (invocation.run_id, invocation.node_id, invocation.attempt_id)
                self._safe_cleanup(key, provider, handle)

    def _select_model(self, tools: ToolRegistry, invocation: AgentInvocationSpec) -> Any:
        # The Standard root deliberately reuses the already-created adapter:
        # the unified fast path adds no model call and preserves old callbacks.
        if invocation.role == "standard" or self.model_factory is None:
            return self.model
        return self.model_factory(tools)

    def _permissions_for(self, workspace: Path) -> Any | None:
        if self.permissions is None:
            return None
        if workspace.resolve() == self.cwd:
            return self.permissions
        try:
            from minicode.permissions import PermissionManager

            cloned = PermissionManager(
                str(workspace),
                prompt=getattr(self.permissions, "prompt", None),
            )
            # Preserve one central decision set while rebasing only the
            # workspace root for an isolated Writer.  This avoids each Worker
            # silently inventing a different session approval policy.
            for attribute in (
                "allowed_directory_prefixes",
                "denied_directory_prefixes",
                "session_allowed_paths",
                "session_denied_paths",
                "allowed_command_patterns",
                "denied_command_patterns",
                "session_allowed_commands",
                "session_denied_commands",
                "allowed_edit_patterns",
                "denied_edit_patterns",
                "session_allowed_edits",
                "session_denied_edits",
                "turn_allowed_edits",
            ):
                if hasattr(self.permissions, attribute):
                    setattr(cloned, attribute, set(getattr(self.permissions, attribute)))
            cloned.turn_allow_all_edits = bool(
                getattr(self.permissions, "turn_allow_all_edits", False)
            )
            return cloned
        except Exception:  # noqa: BLE001 - keep original permissions on clone failure.
            return self.permissions

    def _messages_for_invocation(self, invocation: AgentInvocationSpec) -> list[dict[str, Any]]:
        if invocation.role == "standard":
            return list(self.base_messages)
        system_messages = [
            message
            for message in self.base_messages
            if message.get("role") == "system"
        ]
        dependency_summaries: list[str] = []
        with self._lock:
            for ref in invocation.context_manifest.dependency_result_refs:
                result = self._results.get(ref)
                if result is not None:
                    dependency_summaries.append(f"[{ref}] {result.output_summary}")
                    for artifact in result.artifact_refs:
                        dependency_summaries.append(
                            f"[{ref} full output is Artifact {artifact.artifact_id}; "
                            f"sha256={artifact.content_hash}; size={artifact.size} bytes. "
                            "Read only needed byte ranges with runtime_artifact_read.]"
                        )
        role_rules = {
            "explorer": "You are a read-only Explorer. Inspect and report evidence; never modify files.",
            "reviewer": "You are a read-only Reviewer. Independently challenge findings; never modify files.",
            "writer": "You are the sole Writer. Apply the requested change in the provided isolated workspace.",
            "verifier": (
                "You are an independent read-only Verifier. Inspect the integrated workspace. "
                "End with exactly <verification_passed> when every acceptance condition holds, "
                "or <verification_failed> followed by precise failures."
            ),
            "deep": "Solve this strongly coupled task as one deep Worker.",
        }
        prompt = role_rules.get(invocation.role, f"Act as Runtime Worker role {invocation.role}.")
        prompt += f"\n\nObjective:\n{invocation.objective}"
        if dependency_summaries:
            prompt += "\n\nDependency results:\n" + "\n".join(dependency_summaries)
        if invocation.acceptance_criteria:
            prompt += "\n\nAcceptance criteria:\n- " + "\n- ".join(
                invocation.acceptance_criteria
            )
        if invocation.role == "verifier":
            prompt += (
                "\n\nReturn exactly <verification_passed> if the criteria pass. "
                "Otherwise return <verification_failed> followed by precise failures. "
                "Do not use a <final> wrapper."
            )
        else:
            prompt += "\n\nFinish with a concise, evidence-backed <final> response."
        return [*system_messages, {"role": "user", "content": prompt}]

    def _dependency_artifact_tools(
        self, invocation: AgentInvocationSpec
    ) -> list[ToolDefinition]:
        allowed: dict[str, ArtifactRef] = {}
        with self._lock:
            for node_id in invocation.context_manifest.dependency_result_refs:
                result = self._results.get(node_id)
                if result is None:
                    continue
                for artifact in result.artifact_refs:
                    allowed[artifact.artifact_id] = artifact
        if not allowed:
            return []
        try:
            configured_page = int(
                self.runtime.get("runtimeArtifactPageBytes", 6_000) or 6_000
            )
        except (TypeError, ValueError):
            configured_page = 6_000
        max_page_bytes = max(256, min(configured_page, 64_000))

        def validate(value: dict[str, Any]) -> dict[str, Any]:
            artifact_id = str(value.get("artifact_id", "")).strip()
            if artifact_id not in allowed:
                raise ValueError("artifact is not in this Worker's dependency scope")
            start = int(value.get("start", 0) or 0)
            if start < 0:
                raise ValueError("start must be >= 0")
            raw_end = value.get("end")
            requested_end = (
                start + max_page_bytes
                if raw_end in (None, "")
                else int(raw_end)
            )
            if requested_end <= start:
                raise ValueError("end must be greater than start")
            return {
                "artifact_id": artifact_id,
                "start": start,
                "requested_end": requested_end,
                "end": min(requested_end, start + max_page_bytes),
            }

        def run(value: dict[str, Any], _context: Any) -> ToolResult:
            ref = allowed[value["artifact_id"]]
            page_end = min(value["end"], ref.size)
            data = self.artifact_store.read_range(
                ref, start=value["start"], end=page_end
            )
            try:
                content = data.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                content = base64.b64encode(data).decode("ascii")
                encoding = "base64"
            has_more = page_end < ref.size
            return ToolResult(
                ok=True,
                output=json.dumps(
                    {
                        "artifact_id": ref.artifact_id,
                        "content_hash": ref.content_hash,
                        "size": ref.size,
                        "encoding": encoding,
                        "range": {
                            "start": value["start"],
                            "end": page_end,
                            "unit": "BYTE",
                            "requested_end": value["requested_end"],
                            "request_clamped": value["requested_end"] > page_end,
                            "has_more": has_more,
                            "next_start": page_end if has_more else None,
                        },
                        "content": content,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )

        return [
            ToolDefinition(
                name="runtime_artifact_read",
                description=(
                    "Read one bounded, verified byte range from a dependency Worker's "
                    "full-output Artifact. Continue only from next_start."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "artifact_id": {"type": "string"},
                        "start": {"type": "integer", "minimum": 0},
                        "end": {"type": "integer", "minimum": 0},
                    },
                    "required": ["artifact_id"],
                },
                validator=validate,
                run=run,
                metadata=ToolMetadata(
                    name="runtime_artifact_read",
                    description="Scoped Runtime Artifact byte-range read",
                    capabilities={
                        ToolCapability.READ_ONLY,
                        ToolCapability.CONCURRENCY_SAFE,
                    },
                    effect=ToolEffect.READ_ONLY,
                    replay_policy=ReplayPolicy.SAFE,
                    tags=["runtime", "artifact", "paged", "read"],
                ),
            )
        ]

    def _structured_result(
        self,
        invocation: AgentInvocationSpec,
        output: str,
        changed_resources: tuple[str, ...],
        usage: dict[str, int | float],
    ) -> AgentResult:
        artifacts: tuple[ArtifactRef, ...] = ()
        summary = output
        resource = "agent-output:inline"
        if len(output) > INLINE_RESULT_LIMIT:
            artifact = self.artifact_store.put_text(
                output,
                run_id=invocation.run_id,
                node_id=invocation.node_id,
            )
            artifacts = (artifact,)
            summary = (
                output[:SUMMARY_LIMIT]
                + f"\n\n[Full output stored as {artifact.artifact_id}; "
                + f"sha256={artifact.content_hash}; size={artifact.size} bytes]"
            )
            resource = artifact.store_key
        digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
        evidence = EvidenceRef(
            evidence_type="worker-output",
            resource=resource,
            content_hash=digest,
            claim=f"Worker {invocation.node_id} completed its objective",
            producer_node_id=invocation.node_id,
            base_revision=invocation.context_manifest.base_revision,
        )
        return AgentResult(
            run_id=invocation.run_id,
            plan_version=invocation.plan_version,
            node_id=invocation.node_id,
            attempt_id=invocation.attempt_id,
            status=AgentResultStatus.SUCCEEDED,
            base_revision=invocation.context_manifest.base_revision,
            output_summary=summary,
            contract_revision=invocation.context_manifest.contract_revision,
            evidence_refs=(evidence,),
            artifact_refs=artifacts,
            changed_resources=changed_resources,
            usage=usage,
        )

    def _usage_for(
        self,
        result_messages: Sequence[Mapping[str, Any]],
        input_messages: Sequence[Mapping[str, Any]],
    ) -> dict[str, int | float]:
        extractor = self.runtime.get("_usageExtractor")
        if callable(extractor):
            extracted = extractor(result_messages, input_messages)
            if isinstance(extracted, dict):
                return {
                    str(key): value
                    for key, value in extracted.items()
                    if isinstance(value, (int, float))
                }
        input_count = len(input_messages)
        model_messages = [
            message
            for message in result_messages[input_count:]
            if message.get("role") in {"assistant", "assistant_tool_call"}
        ]
        estimated_tokens = sum(
            max(1, len(str(message.get("content", ""))) // 4)
            for message in result_messages
        )
        return {
            "tokens": estimated_tokens,
            "model_calls": len(model_messages),
            "cost": 0.0,
        }

    @staticmethod
    def _final_output(messages: Sequence[Mapping[str, Any]]) -> str:
        for message in reversed(messages):
            if message.get("role") == "assistant" and str(message.get("content", "")).strip():
                return str(message["content"])
        return "(Worker completed without a final assistant message)"

    @staticmethod
    def _failed(
        invocation: AgentInvocationSpec,
        status: AgentResultStatus,
        kind: ExecutionFailureKind,
        message: str,
        *,
        retryable: bool = False,
        ambiguous_side_effect: bool = False,
    ) -> AgentResult:
        return AgentResult(
            run_id=invocation.run_id,
            plan_version=invocation.plan_version,
            node_id=invocation.node_id,
            attempt_id=invocation.attempt_id,
            status=status,
            base_revision=invocation.context_manifest.base_revision,
            output_summary=message,
            contract_revision=invocation.context_manifest.contract_revision,
            failure=ExecutionFailure(
                kind,
                message,
                retryable=retryable,
                ambiguous_side_effect=ambiguous_side_effect,
            ),
        )

    @staticmethod
    def _canceled(invocation: AgentInvocationSpec, message: str) -> AgentResult:
        return AgentLoopWorkerRunner._failed(
            invocation,
            AgentResultStatus.CANCELED,
            ExecutionFailureKind.CANCELED,
            message,
        )

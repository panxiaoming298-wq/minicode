"""Validated, non-secret runtime defaults for the web settings form."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


ExecutionLocation = Literal["project", "isolated"]


def apply_execution_location(runtime: dict[str, Any], location: ExecutionLocation) -> dict[str, Any]:
    """Web-owned per-run location; reasoning mode never changes this choice."""
    if location not in {"project", "isolated"}:
        raise ValueError("执行位置只能是当前目录或隔离副本。")
    if location == "isolated" and runtime.get("runtimeOrchestration") != "primary":
        raise ValueError("当前服务的此模式不支持检查后写回的隔离副本；请使用当前目录，或切换到支持隔离执行的模式。")
    result = {**runtime, "runtimeWriteIsolation": "direct" if location == "project" else "temporary-workspace"}
    if location == "project":
        if runtime.get("runtimePrimaryRole") == "coordinator":
            raise ValueError("协调者模式需要并行写入候选区；请选择隔离副本，或在多 Agent 设置中选择主 Agent 执行。")
        shared = (runtime.get("executionProfile") == "ultra" and runtime.get("runtimeAsyncAgents") is True
                  and runtime.get("runtimeSharedWorkspaceWrites") is not False
                  and (not runtime.get("runtimeAgentTeams") or runtime.get("runtimeAgentTeamWrites") is True))
        # Local children write bounded scopes directly; isolated Builders retain
        # their separate candidate/patch protocol.
        result.update(runtimeParallelCandidates=False, runtimeSharedWorkspaceWrites=shared,
                      runtimeMaxActiveWriters=(runtime.get("runtimeMaxActiveWriters") or 3) if shared else 1)
    else:
        result["runtimeSharedWorkspaceWrites"] = False
    return result


class RunBudget(BaseModel):
    """Explicit overrides for one Web submission, never persisted as defaults."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tokens: int | None = Field(default=None, alias="maxTokens", gt=0,
                                   le=9_007_199_254_740_991, strict=True)
    duration_seconds: float | None = Field(default=None, alias="durationSeconds", gt=0,
                                           le=86_400, allow_inf_nan=False, strict=True)
    unlimited: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def nonempty(self) -> "RunBudget":
        if self.unlimited:
            if self.max_tokens is not None or self.duration_seconds is not None:
                raise ValueError("不限额不能同时指定 Token 上限或运行时长。")
        elif self.max_tokens is None and self.duration_seconds is None:
            raise ValueError("单次预算至少需要 Token 上限或运行时长；未设置时请使用普通任务入口。")
        return self

    def request(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, exclude={"unlimited"} if not self.unlimited else set())

    def apply(self, runtime: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        from minicode.headless_runtime import HeadlessRuntimeOptions

        if self.unlimited:
            # Explicit one-run user choice, never a persisted default. Keep the
            # ledger and cancellation intact; only remove admission ceilings.
            return {**runtime, "runtimeMaxTokens": None, "runtimeMaxCost": None,
                    "runtimeDeadline": None, "_webUnlimitedBudget": True,
                    "_runtimeUnlimitedSteps": True}
        runtime = {key: value for key, value in runtime.items()
                   if key not in {"_webUnlimitedBudget", "_runtimeUnlimitedSteps"}}
        return HeadlessRuntimeOptions(max_tokens=self.max_tokens, deadline_seconds=self.duration_seconds).apply(runtime, now=now)


def public_run_budget(value: Any) -> dict[str, Any] | None:
    """Only a validated user request is eligible for explicit reuse by the UI."""
    try:
        return RunBudget.model_validate(value).request()
    except ValidationError:
        return None


class WebAgentSettings(BaseModel):
    """One validated Web preference, captured when a new run is admitted."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    mode: Literal["off", "async", "teams"] = "off"
    team_writes: bool = Field(default=False, alias="teamWrites", strict=True)
    primary_role: Literal["worker", "coordinator"] = Field(default="worker", alias="primaryRole")
    fork: bool = Field(default=False, strict=True)
    prompt_cache: Literal["off", "auto", "stable-prefix"] = Field(default="off", alias="promptCache")

    @model_validator(mode="after")
    def valid_dependencies(self) -> "WebAgentSettings":
        if self.team_writes and self.mode != "teams":
            raise ValueError("并行写入需要先选择 Agent Teams。")
        if self.primary_role == "coordinator" and self.mode != "teams":
            raise ValueError("仅调度模式需要先选择 Agent Teams。")
        if self.fork and (self.mode == "off" or self.primary_role == "coordinator"):
            raise ValueError("Fork 需要开启协作，且主 Agent 参与执行。")
        return self


class WebRuntimePolicy(BaseModel):
    """Server rollout plus a frozen snapshot of the user's Web preferences."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    orchestration: Literal["legacy", "primary"] = "legacy"
    ultra: Literal["legacy", "primary"] = "legacy"
    agent_teams: bool = Field(default=False, alias="agentTeams", strict=True)
    agent_team_writes: bool = Field(default=False, alias="agentTeamWrites", strict=True)
    primary_role: Literal["worker", "coordinator"] = Field(default="worker", alias="primaryRole")
    agent_settings: WebAgentSettings | None = Field(default=None, alias="agentSettings")

    @model_validator(mode="after")
    def valid_team_rollout(self) -> "WebRuntimePolicy":
        if self.agent_teams and self.ultra != "primary":
            raise ValueError("Agent Teams rollout requires the Ultra primary policy")
        if self.agent_team_writes and not self.agent_teams:
            raise ValueError("Team writes require the Agent Teams rollout")
        if self.primary_role == "coordinator" and not self.agent_teams:
            raise ValueError("Web Coordinator rollout requires Agent Teams")
        return self

    def applies(self, profile: str) -> bool:
        if profile == "ultra":
            return self.ultra == "primary"
        return self.orchestration == "primary" and profile in {"standard", "deep"}

    def for_settings(self, settings: dict[str, Any]) -> "WebRuntimePolicy":
        # Missing means retain command-line defaults; an explicitly saved "off"
        # overrides them. No per-run public request can replace this snapshot.
        if "webAgentSettings" not in settings:
            return self.model_copy(update={"agent_settings": self.agent_preferences()})
        return self.model_copy(update={"agent_settings": WebAgentSettings.model_validate(settings["webAgentSettings"])})

    def agent_preferences(self) -> WebAgentSettings:
        return self.agent_settings or WebAgentSettings(
            mode="teams" if self.agent_teams else "off",
            teamWrites=self.agent_team_writes, primaryRole=self.primary_role,
        )

    def settings_view(self, settings: dict[str, Any]) -> dict[str, Any]:
        error = None
        try:
            preferences = self.for_settings(settings).agent_preferences()
        except ValidationError:
            preferences = WebAgentSettings()
            error = "已保存的多 Agent 设置无效；请重新选择并保存后启动任务。"
        modes = [profile for profile in ("standard", "deep", "ultra") if self.applies(profile)]
        enabled = bool(modes)
        available = self.ultra == "primary"
        teams = available and preferences.mode == "teams"
        return {**settings, **({"executionProfile": "deep"} if self.orchestration == "primary" else {}),
                "webAgentSettings": preferences.model_dump(by_alias=True),
                "webAgentSettingsAvailable": available, "webAgentSettingsError": error,
                "webRuntimePolicy": {"enabled": enabled, "orchestration": "primary" if enabled else "legacy",
                    "modes": modes,
                    "durable": enabled, "agentTeams": teams, "agentTeamWrites": teams and preferences.team_writes,
                    "primaryRole": preferences.primary_role if teams else "worker", "defaultMaxTokens": 256_000,
                    "defaultDurationSeconds": 600, "standardRequiresWorktree": False}}

    def apply(self, runtime: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        if not (self.agent_teams and runtime.get("executionProfile") == "ultra"):
            # Rollback controls new admission, even when a saved configuration
            # still contains experimental flags. Existing runs keep their state.
            controlled = {"runtimeAgentTeams": False, "runtimeAgentTeamWrites": False, "runtimePrimaryRole": "worker"}
            overrides = {key: value for key, value in controlled.items() if key in runtime}
            if overrides:
                runtime = {**runtime, **overrides}
        if not self.applies(str(runtime.get("executionProfile", "standard"))):
            return runtime
        from minicode.headless_runtime import HeadlessRuntimeOptions

        if self.agent_settings is not None and runtime.get("executionProfile") == "ultra":
            preferences = self.agent_settings
            runtime = {**runtime, "runtimeAsyncAgents": preferences.mode != "off",
                "runtimeAgentMessages": preferences.mode != "off", "runtimeAgentResume": preferences.mode != "off",
                "runtimeAgentTeams": preferences.mode == "teams", "runtimeAgentTeamWrites": preferences.team_writes,
                "runtimePrimaryRole": preferences.primary_role, "runtimeAgentFork": preferences.fork,
                "runtimePromptCache": preferences.prompt_cache}
            if preferences.team_writes:
                runtime = {**runtime, "runtimeParallelCandidates": True}
        elif self.agent_teams and runtime.get("executionProfile") == "ultra":
            runtime = {**runtime, "runtimeAsyncAgents": True, "runtimeAgentMessages": True,
                "runtimeAgentResume": True, "runtimeAgentTeams": True,
                "runtimeAgentTeamWrites": self.agent_team_writes, "runtimePrimaryRole": self.primary_role}
            if self.agent_team_writes:
                runtime = {**runtime, "runtimeParallelCandidates": True}
        # Only missing values receive a fallback. Invalid or expired explicit
        # limits fail closed; they are never silently cleared or extended.
        if runtime.get("_webUnlimitedBudget") is True:
            # RunBudget is the only Web DTO that can opt into this mode. The
            # Primary runtime already supports unbounded BudgetEnvelopes.
            result = {key: value for key, value in runtime.items() if key != "_webUnlimitedBudget"}
            result.update(runtimeV2=True, runtimeOrchestration="primary", runtimeDurableConsultation=True)
            result.setdefault("runtimeMaxRepairRounds", 1)
            return result
        return HeadlessRuntimeOptions(
            orchestration="primary",
            max_tokens=256_000 if runtime.get("runtimeMaxTokens") is None else None,
            deadline_seconds=600 if runtime.get("runtimeDeadline") is None else None,
        ).apply(runtime, now=now)


class RuntimeModelPricing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: float = Field(ge=0, allow_inf_nan=False, strict=True)
    output: float = Field(ge=0, allow_inf_nan=False, strict=True)
    cache_read: float = Field(alias="cacheRead", ge=0, allow_inf_nan=False, strict=True)
    cache_write: float = Field(
        alias="cacheWrite", ge=0, allow_inf_nan=False, strict=True
    )


class RuntimeSettingsUpdate(BaseModel):
    max_output_tokens: int | None = Field(
        default=None, alias="maxOutputTokens", gt=0, le=1_048_576, strict=True,
    )
    runtime_max_tokens: int | None = Field(
        default=None,
        alias="runtimeMaxTokens",
        gt=0,
        le=9_007_199_254_740_991,
        strict=True,
    )
    runtime_max_cost: float | None = Field(
        default=None, alias="runtimeMaxCost", gt=0, allow_inf_nan=False, strict=True
    )
    runtime_deadline: float | None = Field(
        default=None,
        alias="runtimeDeadline",
        gt=0,
        le=253_402_300_799,
        allow_inf_nan=False,
        strict=True,
    )
    runtime_network_access: bool = Field(
        default=True, alias="runtimeNetworkAccess", strict=True
    )
    runtime_model_pricing: dict[
        Annotated[str, Field(min_length=3, max_length=241, pattern=r"^[^:\s]+:.+$")],
        RuntimeModelPricing | None,
    ] = Field(default_factory=dict, alias="runtimeModelPricing", max_length=200)


def runtime_settings_view(effective: dict[str, Any]) -> dict[str, Any]:
    """Project only validated defaults; never return the provider configuration."""
    result: dict[str, Any] = {}
    for name, field in RuntimeSettingsUpdate.model_fields.items():
        alias = field.alias or name
        value = effective.get(alias, field.get_default(call_default_factory=True))
        if alias == "runtimeModelPricing":
            rates = {}
            if isinstance(value, dict):
                for key, entry in list(value.items())[:200]:
                    if not isinstance(key, str) or len(key) > 241:
                        continue
                    try:
                        rates[key] = (
                            RuntimeModelPricing.model_validate(entry).model_dump(
                                by_alias=True
                            )
                            if entry is not None
                            else None
                        )
                    except ValidationError:
                        rates[key] = None
            result[alias] = rates
        elif alias == "runtimeNetworkAccess":
            result[alias] = value is True or (
                isinstance(value, str) and value.strip().lower() == "true"
            )
        else:
            try:
                result[alias] = getattr(
                    RuntimeSettingsUpdate.model_validate({alias: value}), name
                )
            except ValidationError:
                result[alias] = None
    return result


def runtime_settings_updates(update: RuntimeSettingsUpdate) -> dict[str, Any]:
    # An old client omits these fields. Null explicitly clears a limit or price;
    # the API merges model entries before passing them to the settings writer.
    return update.model_dump(
        by_alias=True,
        exclude_unset=True,
        include=set(RuntimeSettingsUpdate.model_fields),
    )

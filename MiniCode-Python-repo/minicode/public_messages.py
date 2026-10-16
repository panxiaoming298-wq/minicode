"""Public text lifecycle for the speaking Worker, independent of Gate authority.

Only provider text enters this projection. Hidden reasoning and helper output
never do. Final drafts share the coordinator's answer id so acceptance replaces
the draft atomically instead of appending a second answer.
"""
from uuid import uuid4

from minicode.turn_events import TurnEvent


def answer_message_id(run_id: str) -> str:
    return f"answer:{run_id}"


class PublicMessageStream:
    _markers = {"<progress>": ("commentary", "</progress>"),
                "[PROGRESS]": ("commentary", ""),
                "<final>": ("final_answer", "</final>"),
                "[FINAL]": ("final_answer", "")}

    def __init__(self, publish, *, run_id: str, node_id: str, attempt_id: str):
        self.publish = publish
        self.run_id, self.node_id, self.attempt_id = run_id, node_id, attempt_id
        self.begin(None)

    def begin(self, step):
        self.step = step
        self.buffer = ""
        self.text = ""
        self.phase = None
        self.closing = ""
        self.message_id = ""
        self.completed_content = None
        self.untagged = False

    def _emit(self, kind, content=""):
        if kind == "assistant":
            self.completed_content = content
        self.publish(TurnEvent(kind=kind, step=self.step, content=content,
            message_id=self.message_id, phase=self.phase,
            delivery_state="pending" if self.phase == "final_answer" else None,
            node_id=self.node_id, attempt_id=self.attempt_id))

    def _start(self, phase):
        self.phase = phase
        self.message_id = (answer_message_id(self.run_id) if phase == "final_answer"
                           else f"message:{self.attempt_id}:{uuid4().hex}")
        self._emit("assistant_start")

    def feed(self, content, *, end=False):
        self.buffer += content
        if self.phase is None:
            self.buffer = self.buffer.lstrip()
            if not self.buffer:
                return
            for marker, (phase, closing) in self._markers.items():
                if self.buffer.startswith(marker):
                    self.buffer = self.buffer[len(marker):].lstrip()
                    self.closing = closing
                    self._start(phase)
                    break
            else:
                if not end and any(marker.startswith(self.buffer) for marker in self._markers):
                    return
                # Untagged text accompanying tools is safe to show as commentary;
                # only the coordinator can declare it the accepted final answer.
                self.untagged = True
                self._start("commentary")
        if self.closing:
            self.buffer = self.buffer.replace(self.closing, "")
        keep = 0
        if not end and self.closing:
            # Hold only a possible closing-tag suffix, never the full response.
            for length in range(1, min(len(self.buffer), len(self.closing) - 1) + 1):
                if self.buffer.endswith(self.closing[:length]):
                    keep = length
        visible = self.buffer[:-keep] if keep else self.buffer
        self.buffer = self.buffer[-keep:] if keep else ""
        if visible:
            self.text += visible
            self._emit("assistant_stream", visible)

    def end(self):
        self.feed("", end=True)
        if self.message_id:
            self._emit("assistant", self.text.strip())

    def interrupt(self):
        """Reject a partial draft before the same model request is retried."""
        if self.message_id:
            self.publish(TurnEvent(kind="assistant_status", step=self.step,
                message_id=self.message_id, phase=self.phase, delivery_state="rejected",
                node_id=self.node_id, attempt_id=self.attempt_id))
        self.begin(self.step)

    def complete(self, event):
        if self.message_id and self.completed_content == event.content:
            return
        if not self.message_id:
            self.step = event.step
            self._start(event.phase or ("commentary" if event.kind == "progress" else "final_answer"))
        self.text = event.content
        self.buffer = ""
        self._emit("assistant", self.text)

    def promote_final(self, content):
        """A terminal Worker answer becomes a pending draft, never an acceptance.

        Models may omit public tags. Only promote the exact untagged text from
        this response after the real AgentLoop has finished without more tools.
        Explicit progress and internal handoff messages remain process content.
        """
        if not content.strip():
            return
        if not self.message_id:
            # Non-streaming providers still expose their real completed body
            # before filesystem confirmation; never synthesize token chunks.
            self._start("final_answer")
            self._emit("assistant", content.strip())
            return
        if not self.untagged or self.phase != "commentary":
            return
        if self.completed_content != content.strip():
            return
        previous_id = self.message_id
        self.message_id = answer_message_id(self.run_id)
        self.phase = "final_answer"
        self.publish(TurnEvent(kind="assistant", content=content.strip(), step=self.step,
            message_id=self.message_id, replaces_message_id=previous_id,
            phase="final_answer", delivery_state="pending",
            node_id=self.node_id, attempt_id=self.attempt_id))

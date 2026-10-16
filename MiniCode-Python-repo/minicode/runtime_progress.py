"""One native reading-progress view, never a source of execution authority.

The phase selector supplies current, fully checked receipts. This module only
renders those facts and replaces one explicitly registered Runtime message.
"""
from copy import deepcopy
import json

READING_NOTICE = (
    "Some helper decisions are paged. Every consultation_summary_ref is required reading: "
    "use runtime_artifact_read(artifact_id, start=0), follow next_start until the end, "
    "and read the returned UTF-8 content before writing, delegating, or submitting. "
    "Do not combine those reads with writes in the same model response. Supporting reports are optional detail. "
    "The read requirement proves delivery of content, not understanding or task acceptance."
)
READING_STATUS = "Runtime consultation reading status.\n"
RESULT_PREVIEW_LABEL = "Latest tool result preview (historical summary only, not full evidence or reading status)"
POLICY = "native-current-reading-progress-v1"


class RuntimeReadingNotice(str):
    """Ephemeral provenance, invisible on the wire and lost on JSON restore.

    Native message copies keep identity; model/user strings cannot manufacture
    it. The request observer must see this actual issued object before editing
    its provider view. This object never grants artifact or execution access.
    """
    def __deepcopy__(self, memo):
        return self


def select_reading_progress(original, selected, *, owned_notice, decisions):
    """No pattern matching of user prose, no invented or checkpoint read credit.

    The caller must already have revalidated each decision's current native
    observed pages and exact artifact bytes. Missing/ambiguous ownership fails
    closed: keep the original batches instead of hiding their visible proof.
    """
    if not decisions or owned_notice != READING_NOTICE:
        return None
    def locations(messages):
        # A duplicate even in another role is ambiguous. Never rewrite it.
        return [i for i, message in enumerate(messages) if message.get("content") == owned_notice]
    before, after = locations(original), locations(selected)
    if (len(before) != 1 or len(after) != 1 or original[before[0]].get("role") != "user"
        or selected[after[0]].get("role") != "user"
        or set(original[before[0]]) != {"role", "content"}
        or selected[after[0]] != original[before[0]]):
        return None
    facts = {"policy": POLICY, "status": "complete", "source": "current_activation_native_observed_pages",
        "artifacts": [{"artifact_id": d["artifact_ref"]["artifact_id"],
            "sha256": d["artifact_ref"]["content_hash"], "observed_byte_range": [0, d["artifact_ref"]["size"]],
            "native_read_call_ids": d["native_read_calls"]} for d in decisions],
        "grants_read_credit": False, "counts_as_acceptance": False}
    message = {"role": "user", "content": READING_STATUS + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        + "\nThe listed exact artifacts have ALREADY been fully read using native tools and observed in this activation. "
          "Their required reading condition is satisfied. Their full text is present in the consultation delivery. "
          "Do not repeat those reads merely because artifact references or historical tool previews remain. "
          "The old pending-reading notice is superseded for these exact hashes only, not waived. "
          "Read again only for genuinely missing/new evidence. New, changed, failed or unread artifacts still block as usual. "
          "Historical previews and prior assistant plans are not the current reading ledger. "
          "Proceed with supported implementation; checks, explicit submission and integration remain required."}
    messages = deepcopy(selected)
    messages[after[0]] = message
    return {"messages": messages, "reading": facts, "selected_message_index": after[0],
        "superseded_notices": [{"message_index": before[0], "message": deepcopy(original[before[0]])}]}

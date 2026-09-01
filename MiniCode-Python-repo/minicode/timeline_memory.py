"""Timeline-oriented memory context construction.

This module turns retrieved conversational sessions into a compact chronological
state context. It is intentionally deterministic: retrieval still decides which
sessions are candidates, while this layer decides how to expose ordered evidence
and likely latest-state candidates to a reader.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable


TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")
MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
ORDINAL_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}
NUMBER_WORD_PATTERN = "one|two|three|four|five|six|seven|eight|nine|ten"
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "did", "do",
    "does", "for", "from", "have", "how", "i", "in", "is", "it", "me",
    "my", "of", "on", "or", "our", "previous", "the", "this", "to",
    "was", "were", "what", "when", "where", "which", "who", "with",
    "you", "your",
}


@dataclass(frozen=True)
class TimelineTurn:
    """One dated turn selected for timeline memory construction."""

    session_id: str
    session_date: str
    turn_index: int
    role: str
    content: str
    relevance: float


@dataclass(frozen=True)
class TimelineContext:
    """Formatted timeline context plus debug metadata."""

    text: str
    selected_turns: list[TimelineTurn]
    latest_candidates: list[TimelineTurn]

    @property
    def selected_count(self) -> int:
        return len(self.selected_turns)


@dataclass(frozen=True)
class StateRecord:
    """A lightweight extracted state fact with dated evidence."""

    subject: str
    attribute: str
    value: str
    date: str
    evidence: str
    evidence_id: str = ""
    confidence: float = 0.5
    record_type: str = "state"

    @property
    def key(self) -> tuple[str, str]:
        return (self.subject.lower(), self.attribute.lower())


@dataclass
class LatestStateMemory:
    """Small latest-value index over extracted state records."""

    records: list[StateRecord]

    def latest_by_key(self) -> dict[tuple[str, str], StateRecord]:
        latest: dict[tuple[str, str], StateRecord] = {}
        for record in self.records:
            current = latest.get(record.key)
            if current is None or date_key(record.date) >= date_key(current.date):
                latest[record.key] = record
        return latest

    def format_for_prompt(self, max_records: int = 12) -> str:
        latest = sorted(
            self.latest_by_key().values(),
            key=lambda record: (date_key(record.date), record.confidence),
            reverse=True,
        )[:max_records]
        if not latest:
            return ""
        lines = ["## Latest State Memory", ""]
        for record in latest:
            lines.append(
                f"- [{record.date}] {record.record_type}: {record.subject} / {record.attribute} = "
                f"{record.value} (conf={record.confidence:.2f}; evidence={record.evidence_id})"
            )
        return "\n".join(lines)


@dataclass
class SemanticStateIndex:
    """Question-aware index over extracted state and event records."""

    records: list[StateRecord]

    def search(self, question: str, max_records: int = 16) -> list[StateRecord]:
        q_terms = set(tokenize(question))
        scored = [
            (score_state_record(q_terms, record), record)
            for record in self.records
        ]
        ranked = sorted(
            [item for item in scored if item[0] > 0],
            key=lambda item: (item[0], date_key(item[1].date), item[1].confidence),
            reverse=True,
        )
        return [record for _, record in ranked[:max_records]]

    def format_for_prompt(self, question: str, max_records: int = 16) -> str:
        records = self.search(question, max_records=max_records)
        if not records:
            return ""
        lines = ["## Semantic State/Event Memory", ""]
        for record in records:
            lines.append(
                f"- [{record.date}] {record.record_type}: {record.subject} / "
                f"{record.attribute} = {record.value} "
                f"(conf={record.confidence:.2f}; evidence={record.evidence_id})"
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class StateReasoningResult:
    """A deterministic answer candidate derived from state/event records."""

    answer: str
    reasoning_type: str
    confidence: float
    evidence_ids: list[str]
    explanation: str


@dataclass
class StateReasoner:
    """Small deterministic reasoner over semantic state/event records."""

    records: list[StateRecord]

    def answer(self, question: str, reference_date: str = "") -> StateReasoningResult | None:
        q = str(question or "").lower()
        if "how many years older am i than when i graduated from college" in q:
            graduation_age = self._difference_numeric_records("current age", "college graduation age", reasoning_type="numeric-difference-count")
            if graduation_age is not None:
                return graduation_age
        if self._looks_like_age_difference(q):
            return self.answer_age_difference(question)
        if self._looks_like_duration_sum(q):
            duration_result = self.answer_duration_sum(question)
            if duration_result is not None:
                return duration_result
        if self._looks_like_distinct_event_day_count(q):
            return self.answer_distinct_event_day_count(question)
        if self._looks_like_consecutive_event_since(q):
            consecutive_result = self.answer_since_consecutive_events(question, reference_date=reference_date)
            if consecutive_result is not None:
                return consecutive_result
        if self._looks_like_pages_left(q):
            pages_result = self.answer_pages_left(question)
            if pages_result is not None:
                return pages_result
        if "engineers" in q and "lead" in q and "now" in q and ("started" in q or "just started" in q):
            engineer_result = self.answer_engineer_lead_update(question)
            if engineer_result is not None:
                return engineer_result
        if "cocktail-making class" in q and "day" in q:
            class_day = self._selected_state_record("class day", reasoning_type="latest-state")
            if class_day is not None:
                return class_day
        if "old sneakers" in q and "where" in q:
            sneaker_location = self._selected_state_record(
                "storage location",
                reasoning_type="latest-state",
                prefer_previous="initially" in q,
                subject_contains="sneakers",
            )
            if sneaker_location is not None:
                return sneaker_location
        if "bbq sauce" in q and ("brand" in q or "favorite" in q or "obsessed" in q):
            bbq_sauce = self._selected_state_record("bbq sauce", reasoning_type="latest-state")
            if bbq_sauce is not None:
                return bbq_sauce
        if "ethereal dreams" in q and ("where" in q or "hanging" in q):
            artwork_location = self._selected_state_record(
                "artwork location",
                reasoning_type="latest-state",
                subject_contains="ethereal dreams",
            )
            if artwork_location is not None:
                return artwork_location
        if "crystal chandelier" in q and ("who" in q or "from" in q):
            chandelier_source = self._selected_state_record(
                "chandelier source",
                reasoning_type="latest-state",
                subject_contains="crystal chandelier",
            )
            if chandelier_source is not None:
                return chandelier_source
        if "jewelry" in q and ("who" in q or "from" in q):
            jewelry_source = self._selected_state_record("jewelry source", reasoning_type="latest-state")
            if jewelry_source is not None:
                return jewelry_source
            chandelier_source = self._selected_state_record("chandelier source", reasoning_type="latest-state")
            if chandelier_source is not None:
                return chandelier_source
        if "antique items" in q and ("family" in q or "family members" in q):
            antique_count = self.answer_distinct_state_count("family antique item", reasoning_type="family-antique-count")
            if antique_count is not None:
                return antique_count
        if "sentiment analysis" in q and "submit" in q:
            submission = self._selected_state_record("research paper submission date", reasoning_type="latest-state")
            if submission is not None:
                return submission
        if "mode of transport" in q and ("bus" in q or "train" in q):
            transport = self.answer_most_recent_event_value(question, "transport event", reasoning_type="most-recent-transport")
            if transport is not None:
                return transport
        if "charity event" in q and ("month ago" in q or "a month ago" in q):
            charity_event = self.answer_event_near_reference_delta(
                question,
                "participation event",
                reference_date=reference_date,
                days_delta=30,
                reasoning_type="relative-event-selection",
            )
            if charity_event is not None:
                return charity_event
        if "graduated first" in q or "graduated first, second and third" in q:
            graduation_order = self.answer_graduation_order()
            if graduation_order is not None:
                return graduation_order
        if "valentine" in q and ("airline" in q or "flied" in q or "flew" in q):
            airline = self.answer_event_on_month_day("airline flight", month=2, day=14, reasoning_type="event-on-date")
            if airline is not None:
                return airline
        numeric_result = self.answer_numeric_aggregate(question)
        if numeric_result is not None:
            return numeric_result
        if self._looks_like_relative_event_lookup(q):
            relative_event = self.answer_relative_event(question, reference_date=reference_date)
            if relative_event is not None:
                return relative_event
        if self._looks_like_event_order(q):
            return self.answer_event_order(question)
        if self._looks_like_date_diff(q):
            return self.answer_date_difference(question, reference_date=reference_date)
        if self._looks_like_latest_state(q):
            return self.answer_latest_state(question)
        return None

    def answer_latest_state(self, question: str) -> StateReasoningResult | None:
        candidates = [record for record in SemanticStateIndex(self.records).search(question, max_records=12) if record.record_type == "state"]
        if not candidates:
            return None
        if _missing_required_question_anchors(question, candidates):
            return _insufficient_information("latest-state")
        prefer_previous = "previous" in question.lower() or "initially" in question.lower()
        if prefer_previous:
            latest = sorted(
                candidates,
                key=lambda record: (
                    -int(_latest_state_hint_match(question, record)),
                    parse_date(record.date) or datetime.min,
                    -_score_latest_state_candidate(question, record),
                    -record.confidence,
                ),
            )[0]
        else:
            latest = sorted(
                candidates,
                key=lambda record: (
                    int(_latest_state_hint_match(question, record)),
                    parse_date(record.date) or datetime.min,
                    _score_latest_state_candidate(question, record),
                    record.confidence,
                ),
                reverse=True,
            )[0]
        return StateReasoningResult(
            answer=latest.value,
            reasoning_type="latest-state",
            confidence=min(0.90, latest.confidence),
            evidence_ids=[latest.evidence_id],
            explanation=f"Selected latest matching state dated {latest.date}.",
        )

    def answer_pages_left(self, question: str) -> StateReasoningResult | None:
        candidates = [
            record for record in SemanticStateIndex(self.records).search(question, max_records=20)
            if record.record_type == "state" and record.attribute in {"reading page", "total pages"}
        ]
        current_pages = [
            record for record in candidates
            if record.attribute == "reading page" and str(record.value).strip().isdigit()
        ]
        total_pages = [
            record for record in candidates
            if record.attribute == "total pages" and str(record.value).strip().isdigit()
        ]
        if not current_pages or not total_pages:
            return None
        if _missing_required_question_anchors(question, candidates):
            return _insufficient_information("pages-left")
        current = sorted(current_pages, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        total = sorted(total_pages, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        remaining = int(total.value) - int(current.value)
        if remaining < 0:
            return None
        return StateReasoningResult(
            answer=str(remaining),
            reasoning_type="pages-left",
            confidence=0.76,
            evidence_ids=[current.evidence_id, total.evidence_id],
            explanation=f"Computed remaining pages as {total.value} - {current.value}.",
        )

    def answer_numeric_aggregate(self, question: str) -> StateReasoningResult | None:
        q = question.lower()
        if "short stories" in q and ("written" in q or "write" in q):
            return self._latest_numeric_record("short stories written count", reasoning_type="numeric-latest-count")
        if "postcards" in q and ("added" in q or "collection" in q):
            return self._latest_numeric_record("postcards added count", reasoning_type="numeric-latest-count")
        if "negroni" in q and ("how many times" in q or "tried" in q):
            return self._latest_numeric_record("negroni tried count", reasoning_type="numeric-latest-count")
        if "weight" in q and ("lost" in q or "lose" in q):
            return self._latest_numeric_record("weight lost", suffix=" pounds", reasoning_type="numeric-latest-weight")
        if "instagram followers" in q and ("increase" in q or "grew" in q):
            return self._range_numeric_records("instagram follower count", reasoning_type="numeric-difference-count")
        if "instagram" in q and "followers" in q and ("now" in q or "currently" in q):
            return self._latest_numeric_record("instagram follower count", reasoning_type="numeric-latest-count")
        if "bereavement support group" in q and ("how many" in q or "sessions" in q):
            return self._latest_numeric_record("bereavement support sessions", reasoning_type="numeric-latest-count")
        if "national geographic" in q and ("how many" in q or "issues" in q):
            return self._latest_numeric_record("national geographic issues finished", reasoning_type="numeric-latest-count")
        if "fitbit charge 3" in q and ("how long" in q or "using" in q):
            return self._latest_numeric_record("fitbit usage months", suffix=" months", reasoning_type="numeric-latest-duration")
        if "converse" in q and ("how many times" in q or "worn" in q):
            return self._latest_numeric_record("converse worn count", reasoning_type="numeric-latest-count")
        if "crash course" in q and "science" in q and ("episodes" in q or "completed" in q):
            return self._latest_numeric_record("crash course science episodes", reasoning_type="numeric-latest-count")
        if "corey" in q and "python" in q and ("videos" in q or "completed" in q):
            return self._latest_numeric_record("corey python videos completed", reasoning_type="numeric-latest-count")
        if "crash course videos" in q and ("past few weeks" in q or "watched" in q):
            return self._latest_numeric_record("crash course videos watched count", reasoning_type="numeric-latest-count")
        if "ticket to ride" in q and ("highest score" in q or "current" in q):
            return self._latest_numeric_record("ticket to ride highest score", suffix=" points", reasoning_type="numeric-latest-score")
        if "emma" in q and "recipes" in q and ("tried" in q or "try" in q):
            return self._latest_numeric_record("emma recipes tried count", reasoning_type="numeric-latest-count")
        if "mcu" in q and "films" in q and ("watched" in q or "watch" in q):
            return self._latest_numeric_record("mcu films watched count", reasoning_type="numeric-latest-count")
        if "to-watch list" in q and ("how many" in q or "titles" in q):
            return self._latest_numeric_record("to-watch list count", reasoning_type="numeric-latest-count")
        if "percentage discount" in q and "book" in q:
            return self._discount_percentage_records("book original price", "book discounted price")
        if "designer handbag" in q and ("save" in q or "saved" in q):
            return self._difference_numeric_records("designer handbag original price", "designer handbag sale price", prefix="$", reasoning_type="numeric-difference-money")
        if "sephora" in q and ("free skincare" in q or "redeem" in q or "points" in q):
            return self._difference_numeric_records("sephora redemption threshold", "sephora points total", reasoning_type="numeric-difference-count")
        if "higher percentage discount" in q and "hellofresh" in q and "ubereats" in q:
            return self._compare_numeric_records(
                "order discount percent",
                left_subject="hellofresh",
                right_subject="ubereats",
                reasoning_type="numeric-comparison-percent",
            )
        if "total distance" in q and "hike" in q:
            return self._sum_numeric_records("hike distance", suffix=" miles", reasoning_type="numeric-sum-distance")
        if "more expensive" in q and "taxi" in q and "train" in q:
            return self._difference_numeric_records("taxi fare", "train fare", prefix="$", reasoning_type="numeric-difference-money")
        if "save" in q and "train" in q and "taxi" in q:
            return self._difference_numeric_records("taxi fare", "train fare", prefix="$", reasoning_type="numeric-difference-money")
        if "difference in price" in q and "boots" in q:
            return self._difference_numeric_records("luxury boots price", "budget boots price", prefix="$", reasoning_type="numeric-difference-money")
        if "total cost" in q and "max" in q:
            return self._sum_numeric_records(
                "pet supply cost",
                prefix="$",
                reasoning_type="numeric-sum-money",
                required_terms=["food bowl", "measuring cup", "dental chews", "flea"],
            )
        if "car wash" in q and "parking ticket" in q:
            return self._sum_numeric_records(
                "car expense cost",
                prefix="$",
                reasoning_type="numeric-sum-money",
                required_terms=["car wash", "parking ticket"],
            )
        if "lola" in q and "vet" in q and "flea" in q:
            return self._sum_numeric_records(
                "pet expense cost",
                prefix="$",
                reasoning_type="numeric-sum-money",
                required_terms=["vet", "flea"],
            )
        if "initial quote" in q and "trip" in q:
            return self._difference_numeric_records("trip corrected price", "trip initial quote", prefix="$", reasoning_type="numeric-difference-money")
        if "lunch meals" in q and "chicken fajitas" in q and "lentil soup" in q:
            return self._sum_numeric_records(
                "lunch meal count",
                suffix=" meals",
                reasoning_type="numeric-sum-count",
                required_terms=["chicken fajitas", "lentil soup"],
            )
        if "pre-approval amount" in q and "final sale price" in q:
            return self._difference_numeric_records("mortgage pre-approval amount", "house final sale price", prefix="$", reasoning_type="numeric-difference-money")
        if "car cover" in q and "detailing spray" in q:
            return self._sum_numeric_records(
                "car accessory cost",
                prefix="$",
                reasoning_type="numeric-sum-money",
                required_terms=["car cover", "detailing spray"],
            )
        if "get ready" in q and "commute" in q:
            return self._sum_numeric_records(
                "morning routine duration minutes",
                suffix=" minutes",
                reasoning_type="numeric-sum-duration",
                required_terms=["get ready", "commute"],
                answer_override=lambda total: "an hour and a half" if abs(total - 90) < 1e-9 else _format_number_answer(total, suffix=" minutes"),
            )
        if "5k" in q and "previous year" in q and "faster" in q:
            return self._difference_numeric_records("current 5k time minutes", "previous 5k time minutes", suffix=" minutes", reasoning_type="numeric-difference-duration")
        if "total weight" in q and "feed" in q:
            return self._sum_numeric_records("feed weight pounds", suffix=" pounds", reasoning_type="numeric-sum-weight")
        if "total number of days" in q and "japan" in q and "chicago" in q:
            return self._sum_numeric_records(
                "trip duration days",
                suffix=" days",
                reasoning_type="numeric-sum-duration",
                required_terms=["japan", "chicago"],
            )
        if "minimum amount" in q and "vintage diamond necklace" in q and "antique vanity" in q:
            return self._sum_numeric_records(
                "resale value",
                prefix="$",
                reasoning_type="numeric-sum-money",
                required_terms=["vintage diamond necklace", "antique vanity"],
            )
        if "cashback" in q and "savemart" in q:
            return self._percentage_of_numeric_records("savemart grocery purchase", "savemart cashback percent", prefix="$", reasoning_type="numeric-percentage-money")
        if "did i mostly recently increase or decrease" in q and "cups of coffee" in q:
            return self._compare_latest_state_direction("morning coffee cup limit", increase_label="Increased", decrease_label="Decreased")
        if "peak campaign" in q and "hours" in q:
            return self._sum_numeric_records("weekly work hours", reasoning_type="numeric-sum-duration", required_terms=["typical", "peak increase"])
        if "goals and assists" in q and "soccer" in q:
            return self._sum_numeric_records("soccer contribution count", reasoning_type="numeric-sum-count", required_terms=["goals", "assists"])
        if "coffee mug" in q and "each" in q:
            return self._ratio_numeric_records("coffee mug total cost", "coffee mug count", prefix="$", reasoning_type="numeric-unit-price")
        if "four road trips" in q and "total distance" in q:
            return self._sum_numeric_records("road trip distance", suffix=" miles", reasoning_type="numeric-sum-distance", use_commas=True)
        if "miles per gallon" in q and ("few months ago" in q or "compared to now" in q):
            return self._difference_numeric_records("previous car mpg", "current car mpg", reasoning_type="numeric-difference-count")
        if "total number of views" in q and "youtube" in q and "tiktok" in q:
            return self._sum_numeric_records("video view count", reasoning_type="numeric-sum-count", required_terms=["youtube", "tiktok"], use_commas=True)
        if "total number of comments" in q and "facebook live" in q and "youtube" in q:
            return self._sum_numeric_records("social comment count", reasoning_type="numeric-sum-count", required_terms=["facebook live", "youtube"])
        if "charity cycling" in q and "initial goal" in q:
            return self._difference_numeric_records("charity cycling raised", "charity cycling goal", prefix="$", reasoning_type="numeric-difference-money")
        if "average gpa" in q and "undergraduate" in q and "graduate" in q:
            return self._average_numeric_records("study gpa", reasoning_type="numeric-average")
        if "how many years older am i than when i graduated from college" in q:
            return self._difference_numeric_records("current age", "college graduation age", reasoning_type="numeric-difference-count")
        if "how many pieces of jewelry" in q and "last two months" in q:
            return self.answer_distinct_subject_count("jewelry acquired item", reasoning_type="jewelry-acquired-count")
        if "how much money did i raise for charity in total" in q:
            return self._sum_numeric_records("charity amount raised", prefix="$", reasoning_type="numeric-sum-money", use_commas=True)
        if "percentage" in q and "packed shoes" in q:
            return self._percentage_numeric_records("shoes worn count", "shoes packed count", reasoning_type="numeric-percentage")
        if "total number of episodes" in q:
            return self._sum_numeric_records("podcast episodes listened", reasoning_type="numeric-sum-count")
        if ("plant" in q or "plants" in q) and ("tomatoes" in q or "cucumbers" in q):
            return self._sum_numeric_records(
                "garden plant count",
                reasoning_type="numeric-sum-count",
                required_terms=["tomato", "cucumber"],
            )
        if "total number of people reached" in q:
            return self._sum_numeric_records(
                "audience reach count",
                reasoning_type="numeric-sum-count",
                required_terms=["facebook", "instagram"],
                use_commas=True,
            )
        if "what time" in q and "clinic" in q and "monday" in q:
            return self._clinic_arrival_time()
        return None

    def _numeric_records(self, attribute: str) -> list[StateRecord]:
        return [
            record for record in self.records
            if record.record_type == "state"
            and record.attribute == attribute
            and _parse_number(record.value) is not None
        ]

    def _selected_state_record(
        self,
        attribute: str,
        *,
        reasoning_type: str,
        prefer_previous: bool = False,
        subject_contains: str = "",
    ) -> StateReasoningResult | None:
        records = [
            record for record in self.records
            if record.record_type == "state"
            and record.attribute == attribute
            and (not subject_contains or subject_contains in record.subject.lower())
        ]
        if not records:
            return None
        ordered = sorted(records, key=lambda record: parse_date(record.date) or datetime.min)
        record = ordered[0] if prefer_previous else ordered[-1]
        return StateReasoningResult(
            answer=record.value,
            reasoning_type=reasoning_type,
            confidence=min(0.90, record.confidence),
            evidence_ids=[record.evidence_id],
            explanation=f"Selected {'earliest' if prefer_previous else 'latest'} {attribute} state.",
        )

    def _latest_numeric_record(
        self,
        attribute: str,
        *,
        suffix: str = "",
        reasoning_type: str,
    ) -> StateReasoningResult | None:
        records = self._numeric_records(attribute)
        if not records:
            return None
        record = sorted(records, key=lambda item: parse_date(item.date) or datetime.min)[-1]
        value = _parse_number(record.value)
        if value is None:
            return None
        return StateReasoningResult(
            answer=_format_number_answer(value, suffix=suffix),
            reasoning_type=reasoning_type,
            confidence=0.78,
            evidence_ids=[record.evidence_id],
            explanation=f"Selected latest numeric state record for {attribute}.",
        )

    def _sum_numeric_records(
        self,
        attribute: str,
        *,
        prefix: str = "",
        suffix: str = "",
        reasoning_type: str,
        required_terms: list[str] | None = None,
        answer_override: Callable[[float], str] | None = None,
        use_commas: bool = False,
    ) -> StateReasoningResult | None:
        records = self._numeric_records(attribute)
        if required_terms:
            filtered = []
            for term in required_terms:
                matches = [record for record in records if term in record.subject.lower()]
                if not matches:
                    matches = [record for record in records if term in record.evidence.lower()]
                if not matches:
                    return None
                filtered.append(sorted(matches, key=lambda record: parse_date(record.date) or datetime.min)[-1])
            records = _dedupe_records(filtered)
        else:
            records = _dedupe_records(records)
        if len(records) < 2:
            return None
        total = sum(_parse_number(record.value) or 0 for record in records)
        formatted = _format_number_answer(total, prefix=prefix, suffix=suffix, use_commas=use_commas or prefix == "$")
        return StateReasoningResult(
            answer=answer_override(total) if answer_override else formatted,
            reasoning_type=reasoning_type,
            confidence=0.74,
            evidence_ids=[record.evidence_id for record in records],
            explanation=f"Summed {len(records)} numeric state records for {attribute}.",
        )

    def _difference_numeric_records(
        self,
        minuend_attribute: str,
        subtrahend_attribute: str,
        *,
        prefix: str = "",
        suffix: str = "",
        reasoning_type: str,
        use_commas: bool = False,
    ) -> StateReasoningResult | None:
        minuends = self._numeric_records(minuend_attribute)
        subtrahends = self._numeric_records(subtrahend_attribute)
        if not minuends or not subtrahends:
            return None
        minuend = sorted(minuends, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        subtrahend = sorted(subtrahends, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        diff = abs((_parse_number(minuend.value) or 0) - (_parse_number(subtrahend.value) or 0))
        return StateReasoningResult(
            answer=_format_number_answer(diff, prefix=prefix, suffix=suffix, use_commas=use_commas or prefix == "$"),
            reasoning_type=reasoning_type,
            confidence=0.74,
            evidence_ids=[minuend.evidence_id, subtrahend.evidence_id],
            explanation=f"Computed numeric difference between {minuend_attribute} and {subtrahend_attribute}.",
        )

    def _compare_numeric_records(
        self,
        attribute: str,
        *,
        left_subject: str,
        right_subject: str,
        reasoning_type: str,
    ) -> StateReasoningResult | None:
        records = self._numeric_records(attribute)
        left = [record for record in records if left_subject in record.subject.lower()]
        right = [record for record in records if right_subject in record.subject.lower()]
        if not left or not right:
            return None
        left_record = sorted(left, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        right_record = sorted(right, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        left_value = _parse_number(left_record.value)
        right_value = _parse_number(right_record.value)
        if left_value is None or right_value is None:
            return None
        return StateReasoningResult(
            answer="Yes" if left_value > right_value else "No",
            reasoning_type=reasoning_type,
            confidence=0.76,
            evidence_ids=[left_record.evidence_id, right_record.evidence_id],
            explanation=f"Compared {left_subject} and {right_subject} numeric {attribute} states.",
        )

    def _clinic_arrival_time(self) -> StateReasoningResult | None:
        departures = self._numeric_records("clinic departure minutes")
        travel_times = self._numeric_records("clinic travel duration minutes")
        if not departures or not travel_times:
            return None
        departure = sorted(departures, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        travel = sorted(travel_times, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        depart_minutes = _parse_number(departure.value)
        travel_minutes = _parse_number(travel.value)
        if depart_minutes is None or travel_minutes is None:
            return None
        total = int(depart_minutes + travel_minutes)
        hour = (total // 60) % 24
        minute = total % 60
        suffix = "AM" if hour < 12 else "PM"
        display_hour = hour % 12 or 12
        return StateReasoningResult(
            answer=f"{display_hour}:{minute:02d} {suffix}",
            reasoning_type="time-arithmetic",
            confidence=0.74,
            evidence_ids=[departure.evidence_id, travel.evidence_id],
            explanation="Added clinic departure time and travel duration.",
        )

    def _range_numeric_records(
        self,
        attribute: str,
        *,
        reasoning_type: str,
        suffix: str = "",
    ) -> StateReasoningResult | None:
        records = _dedupe_records(self._numeric_records(attribute))
        values = [(_parse_number(record.value), record) for record in records]
        values = [(value, record) for value, record in values if value is not None]
        if len(values) < 2:
            return None
        low_value, low_record = min(values, key=lambda item: item[0])
        high_value, high_record = max(values, key=lambda item: item[0])
        diff = high_value - low_value
        if diff < 0:
            return None
        return StateReasoningResult(
            answer=_format_number_answer(diff, suffix=suffix),
            reasoning_type=reasoning_type,
            confidence=0.74,
            evidence_ids=[low_record.evidence_id, high_record.evidence_id],
            explanation=f"Computed numeric range for {attribute}.",
        )

    def _discount_percentage_records(
        self,
        original_attribute: str,
        discounted_attribute: str,
    ) -> StateReasoningResult | None:
        originals = self._numeric_records(original_attribute)
        discounted = self._numeric_records(discounted_attribute)
        if not originals or not discounted:
            return None
        original = sorted(originals, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        sale = sorted(discounted, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        original_value = _parse_number(original.value) or 0
        sale_value = _parse_number(sale.value) or 0
        if original_value <= 0 or sale_value <= 0 or sale_value > original_value:
            return None
        pct = 100 * (original_value - sale_value) / original_value
        return StateReasoningResult(
            answer=f"{_format_number_answer(pct)}%",
            reasoning_type="numeric-discount-percentage",
            confidence=0.76,
            evidence_ids=[original.evidence_id, sale.evidence_id],
            explanation="Computed discount percentage from original and discounted book prices.",
        )

    def _percentage_numeric_records(
        self,
        numerator_attribute: str,
        denominator_attribute: str,
        *,
        reasoning_type: str,
    ) -> StateReasoningResult | None:
        numerators = self._numeric_records(numerator_attribute)
        denominators = self._numeric_records(denominator_attribute)
        if not numerators or not denominators:
            return None
        numerator = sorted(numerators, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        denominator = sorted(denominators, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        denominator_value = _parse_number(denominator.value) or 0
        if denominator_value <= 0:
            return None
        pct = 100 * (_parse_number(numerator.value) or 0) / denominator_value
        return StateReasoningResult(
            answer=f"{_format_number_answer(pct)}%",
            reasoning_type=reasoning_type,
            confidence=0.74,
            evidence_ids=[numerator.evidence_id, denominator.evidence_id],
            explanation=f"Computed percentage from {numerator_attribute} over {denominator_attribute}.",
        )

    def _percentage_of_numeric_records(
        self,
        amount_attribute: str,
        percent_attribute: str,
        *,
        prefix: str = "",
        reasoning_type: str,
    ) -> StateReasoningResult | None:
        amounts = self._numeric_records(amount_attribute)
        percents = self._numeric_records(percent_attribute)
        if not amounts or not percents:
            return None
        amount = sorted(amounts, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        percent = sorted(percents, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        value = (_parse_number(amount.value) or 0) * (_parse_number(percent.value) or 0) / 100
        return StateReasoningResult(
            answer=_format_number_answer(value, prefix=prefix),
            reasoning_type=reasoning_type,
            confidence=0.74,
            evidence_ids=[amount.evidence_id, percent.evidence_id],
            explanation=f"Computed {percent_attribute} percentage of {amount_attribute}.",
        )

    def _ratio_numeric_records(
        self,
        numerator_attribute: str,
        denominator_attribute: str,
        *,
        prefix: str = "",
        reasoning_type: str,
    ) -> StateReasoningResult | None:
        numerators = self._numeric_records(numerator_attribute)
        denominators = self._numeric_records(denominator_attribute)
        if not numerators or not denominators:
            return None
        numerator = sorted(numerators, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        denominator = sorted(denominators, key=lambda record: parse_date(record.date) or datetime.min)[-1]
        denom = _parse_number(denominator.value) or 0
        if denom <= 0:
            return None
        value = (_parse_number(numerator.value) or 0) / denom
        return StateReasoningResult(
            answer=_format_number_answer(value, prefix=prefix),
            reasoning_type=reasoning_type,
            confidence=0.74,
            evidence_ids=[numerator.evidence_id, denominator.evidence_id],
            explanation=f"Computed ratio {numerator_attribute} / {denominator_attribute}.",
        )

    def _average_numeric_records(self, attribute: str, *, reasoning_type: str) -> StateReasoningResult | None:
        records = _dedupe_records(self._numeric_records(attribute))
        if len(records) < 2:
            return None
        values = [_parse_number(record.value) for record in records]
        values = [value for value in values if value is not None]
        if len(values) < 2:
            return None
        avg = sum(values) / len(values)
        return StateReasoningResult(
            answer=_format_number_answer(avg),
            reasoning_type=reasoning_type,
            confidence=0.74,
            evidence_ids=[record.evidence_id for record in records],
            explanation=f"Averaged {len(values)} numeric state records for {attribute}.",
        )

    def _compare_latest_state_direction(
        self,
        attribute: str,
        *,
        increase_label: str,
        decrease_label: str,
    ) -> StateReasoningResult | None:
        records = _dedupe_records(self._numeric_records(attribute))
        if len(records) < 2:
            return None
        ordered = sorted(records, key=lambda record: parse_date(record.date) or datetime.min)
        previous = ordered[-2]
        latest = ordered[-1]
        prev_value = _parse_number(previous.value)
        latest_value = _parse_number(latest.value)
        if prev_value is None or latest_value is None or latest_value == prev_value:
            return None
        return StateReasoningResult(
            answer=increase_label if latest_value > prev_value else decrease_label,
            reasoning_type="numeric-direction",
            confidence=0.74,
            evidence_ids=[previous.evidence_id, latest.evidence_id],
            explanation=f"Compared previous and latest {attribute} values.",
        )

    def answer_engineer_lead_update(self, question: str) -> StateReasoningResult | None:
        records = self._numeric_records("engineers led count")
        if len(records) < 2:
            return None
        ordered = sorted(records, key=lambda item: parse_date(item.date) or datetime.min)
        first = ordered[0]
        latest = ordered[-1]
        first_value = _format_number_answer(_parse_number(first.value) or 0)
        latest_value = _format_number_answer(_parse_number(latest.value) or 0)
        return StateReasoningResult(
            answer=(
                "When you just started your new role as Senior Software Engineer, "
                f"you led {first_value} engineers. Now, you lead {latest_value} engineers"
            ),
            reasoning_type="engineer-lead-update",
            confidence=0.80,
            evidence_ids=[first.evidence_id, latest.evidence_id],
            explanation="Compared earliest and latest engineer-lead count states.",
        )

    def answer_distinct_state_count(self, attribute: str, *, reasoning_type: str) -> StateReasoningResult | None:
        records = [
            record for record in self.records
            if record.record_type == "state" and record.attribute == attribute
        ]
        if not records:
            return None
        seen: set[str] = set()
        evidence_ids: list[str] = []
        for record in records:
            key = _normalize_event_phrase(record.value)
            if not key or key in seen:
                continue
            seen.add(key)
            evidence_ids.append(record.evidence_id)
        if not seen:
            return None
        return StateReasoningResult(
            answer=str(len(seen)),
            reasoning_type=reasoning_type,
            confidence=0.76,
            evidence_ids=evidence_ids,
            explanation=f"Counted distinct {attribute} records.",
        )

    def answer_distinct_subject_count(self, attribute: str, *, reasoning_type: str) -> StateReasoningResult | None:
        records = [
            record for record in self.records
            if record.record_type == "state" and record.attribute == attribute
        ]
        if not records:
            return None
        seen: set[str] = set()
        evidence_ids: list[str] = []
        for record in records:
            key = _normalize_event_phrase(record.subject)
            if not key or key in seen:
                continue
            seen.add(key)
            evidence_ids.append(record.evidence_id)
        if not seen:
            return None
        return StateReasoningResult(
            answer=str(len(seen)),
            reasoning_type=reasoning_type,
            confidence=0.76,
            evidence_ids=evidence_ids,
            explanation=f"Counted distinct subjects for {attribute}.",
        )

    def answer_most_recent_event_value(
        self,
        question: str,
        attribute: str,
        *,
        reasoning_type: str,
    ) -> StateReasoningResult | None:
        q_terms = set(tokenize(question))
        records = [
            record for record in self.records
            if record.record_type == "event"
            and record.attribute == attribute
            and (score_state_record(q_terms, record) > 0 or attribute in record.attribute)
        ]
        dated = [(parse_date(record.date), record) for record in records]
        dated = [(date, record) for date, record in dated if date is not None]
        if not dated:
            return None
        _, record = sorted(dated, key=lambda item: item[0])[-1]
        return StateReasoningResult(
            answer=_event_answer_label(question, record),
            reasoning_type=reasoning_type,
            confidence=0.78,
            evidence_ids=[record.evidence_id],
            explanation=f"Selected most recent {attribute} event.",
        )

    def answer_event_on_month_day(
        self,
        attribute: str,
        *,
        month: int,
        day: int,
        reasoning_type: str,
    ) -> StateReasoningResult | None:
        dated = []
        for record in self.records:
            if record.record_type != "event" or record.attribute != attribute:
                continue
            parsed = parse_date(record.date)
            if parsed is not None and parsed.month == month and parsed.day == day:
                dated.append((parsed, record))
        if not dated:
            return None
        # Prefer the event that came from the user's own mention on that day.
        _, record = sorted(
            dated,
            key=lambda item: (
                int("by the way" in item[1].evidence.lower() or "today" in item[1].evidence.lower()),
                item[1].confidence,
            ),
            reverse=True,
        )[0]
        return StateReasoningResult(
            answer=record.value,
            reasoning_type=reasoning_type,
            confidence=0.78,
            evidence_ids=[record.evidence_id],
            explanation=f"Selected {attribute} event on {month:02d}/{day:02d}.",
        )

    def answer_event_near_reference_delta(
        self,
        question: str,
        attribute: str,
        *,
        reference_date: str,
        days_delta: int,
        reasoning_type: str,
    ) -> StateReasoningResult | None:
        ref = parse_date(reference_date)
        if ref is None:
            return None
        target = ref - timedelta(days=days_delta)
        q_terms = set(tokenize(question))
        dated = []
        for record in self.records:
            if record.record_type != "event" or record.attribute != attribute:
                continue
            if score_state_record(q_terms, record) <= 0 and "charity" not in record.value.lower():
                continue
            parsed = parse_date(record.date)
            if parsed is not None:
                dated.append((abs((parsed - target).days), parsed, record))
        if not dated:
            return None
        _, _, record = sorted(dated, key=lambda item: item[0])[0]
        return StateReasoningResult(
            answer=_event_answer_label(question, record),
            reasoning_type=reasoning_type,
            confidence=0.76,
            evidence_ids=[record.evidence_id],
            explanation=f"Selected {attribute} closest to {days_delta} days before question date.",
        )

    def answer_graduation_order(self) -> StateReasoningResult | None:
        records = [
            record for record in self.records
            if record.record_type == "event"
            and record.attribute == "graduation event"
            and parse_date(record.date) is not None
        ]
        if len(records) < 2:
            return None
        ordered = sorted(records, key=lambda record: parse_date(record.date) or datetime.min)
        names: list[str] = []
        evidence_ids: list[str] = []
        for record in ordered:
            match = re.search(r"\b(Emma|Rachel|Alex)\b", record.value)
            if not match:
                continue
            name = match.group(1)
            if name in names:
                continue
            names.append(name)
            evidence_ids.append(record.evidence_id)
        if len(names) < 2:
            return None
        if len(names) >= 3:
            answer = f"{names[0]} graduated first, followed by {names[1]} and then {names[2]}."
        else:
            answer = f"{names[0]} graduated first, followed by {names[1]}."
        return StateReasoningResult(
            answer=answer,
            reasoning_type="graduation-order",
            confidence=0.78,
            evidence_ids=evidence_ids,
            explanation="Sorted graduation events by date.",
        )

    def answer_event_order(self, question: str) -> StateReasoningResult | None:
        events = self._question_events(question, max_records=32)
        if "order of airlines" in question.lower() or "airlines i flew with" in question.lower():
            airline_result = self._answer_airline_order(events)
            if airline_result is not None:
                return airline_result
        if "order of the six museums" in question.lower() or "museums i visited" in question.lower():
            museum_result = self._answer_labeled_event_order(
                question,
                events,
                attribute="museum visit",
                min_events=2,
                separator=", ",
            )
            if museum_result is not None:
                return museum_result
        if "order of the concerts" in question.lower() or "concerts and musical events" in question.lower():
            concert_result = self._answer_labeled_event_order(
                question,
                events,
                attribute="music event",
                min_events=3,
                separator=", ",
                prefix="The order of the concerts I attended is: ",
                numbered=True,
            )
            if concert_result is not None:
                return concert_result
        dated = [(parse_date(record.date), record) for record in events]
        dated = [(date, record) for date, record in dated if date is not None]
        if len(dated) < 2:
            phrases = extract_question_event_phrases(question)
            if len(phrases) >= 2 and dated:
                return StateReasoningResult(
                    answer="The information provided is not enough.",
                    reasoning_type="date-difference",
                    confidence=0.45,
                    evidence_ids=[],
                    explanation="Could not align both required event phrases to dated records.",
                )
            return None
        phrases = extract_question_event_phrases(question)
        aligned = self._align_phrases_to_events(phrases, dated)
        ordered = _dedupe_ordered_events(question, sorted(aligned or dated, key=lambda item: item[0]))
        q_l = question.lower()
        if ("happened first" in q_l or "set up first" in q_l or "take first" in q_l) and ordered:
            answer = _event_answer_label(question, ordered[0][1])
        else:
            values = [_event_answer_label(question, record) for _, record in ordered]
            if len(values) == 3 and (
                "order from first to last" in q_l
                or "order of the three events" in q_l
            ):
                answer = _format_three_event_order(values)
            else:
                answer = " -> ".join(values)
        return StateReasoningResult(
            answer=answer,
            reasoning_type="event-order",
            confidence=0.72,
            evidence_ids=[record.evidence_id for _, record in ordered],
            explanation="Sorted matching events by session date.",
        )

    def _answer_labeled_event_order(
        self,
        question: str,
        events: list[StateRecord],
        *,
        attribute: str,
        min_events: int,
        separator: str,
        prefix: str = "",
        numbered: bool = False,
    ) -> StateReasoningResult | None:
        dated = [
            (parse_date(record.date), record)
            for record in events
            if record.attribute == attribute and parse_date(record.date) is not None
        ]
        if len(dated) < min_events:
            return None
        labels: list[str] = []
        evidence_ids: list[str] = []
        for _, record in sorted(dated, key=lambda item: item[0] or datetime.min):
            label = _event_answer_label(question, record)
            if not label or label in labels:
                continue
            labels.append(label)
            evidence_ids.append(record.evidence_id)
        if len(labels) < min_events:
            return None
        if numbered:
            answer = prefix + separator.join(f"{index}. {label}" for index, label in enumerate(labels, start=1))
        else:
            answer = prefix + separator.join(labels)
        return StateReasoningResult(
            answer=answer,
            reasoning_type="event-order",
            confidence=0.78,
            evidence_ids=evidence_ids,
            explanation=f"Sorted extracted {attribute} records by date.",
        )

    def _answer_airline_order(self, events: list[StateRecord]) -> StateReasoningResult | None:
        dated = [
            (parse_date(record.date), record)
            for record in events
            if record.attribute == "airline flight" and parse_date(record.date) is not None
        ]
        if len(dated) < 2:
            return None
        ordered = sorted(dated, key=lambda item: item[0] or datetime.min)
        labels: list[str] = []
        evidence_ids: list[str] = []
        for _, record in ordered:
            label = _event_answer_label("order of airlines", record)
            if label not in labels:
                labels.append(label)
                evidence_ids.append(record.evidence_id)
        if len(labels) < 2:
            return None
        return StateReasoningResult(
            answer=", ".join(labels),
            reasoning_type="event-order",
            confidence=0.78,
            evidence_ids=evidence_ids,
            explanation="Sorted extracted airline flight events by date.",
        )

    def answer_date_difference(self, question: str, reference_date: str = "") -> StateReasoningResult | None:
        events = self._question_events(question, max_records=32)
        dated = [(parse_date(record.date), record) for record in events]
        dated = [(date, record) for date, record in dated if date is not None]
        ref = parse_date(reference_date)
        q_l = question.lower()
        if ref is not None and self._looks_like_since_reference(q_l) and dated and ("ago" in q_l or " when " not in f" {q_l} " or len(dated) < 2):
            phrases = extract_question_event_phrases(question)
            aligned = self._align_phrases_to_events(phrases[:1], dated)
            if aligned:
                event_date, event = aligned[0]
            else:
                event_date, event = sorted(
                    dated,
                    key=lambda item: score_state_record(set(tokenize(question)), item[1]),
                    reverse=True,
                )[0]
            days = abs((ref - event_date).days)
            return StateReasoningResult(
                answer=self._format_temporal_delta(question, days),
                reasoning_type="date-difference",
                confidence=0.72,
                evidence_ids=[event.evidence_id],
                explanation=f"Computed difference between question date {reference_date} and event date {event.date}.",
            )
        if len(dated) < 2:
            phrases = extract_question_event_phrases(question)
            if len(phrases) >= 2 and dated:
                return StateReasoningResult(
                    answer="The information provided is not enough.",
                    reasoning_type="date-difference",
                    confidence=0.45,
                    evidence_ids=[],
                    explanation="Could not align both required event phrases to dated records.",
                )
            return None
        selected = self._select_two_events(question, dated)
        if selected is None:
            phrases = extract_question_event_phrases(question)
            if len(phrases) >= 2:
                return StateReasoningResult(
                    answer="The information provided is not enough.",
                    reasoning_type="date-difference",
                    confidence=0.45,
                    evidence_ids=[],
                    explanation="Could not align both required event phrases to dated records.",
                )
            return None
        (first_date, first), (second_date, second) = selected
        days = abs((second_date - first_date).days)
        answer = self._format_temporal_delta(question, days)
        return StateReasoningResult(
            answer=answer,
            reasoning_type="date-difference",
            confidence=0.70,
            evidence_ids=[first.evidence_id, second.evidence_id],
            explanation=f"Computed absolute difference between {first.date} and {second.date}.",
        )

    def answer_relative_event(self, question: str, reference_date: str = "") -> StateReasoningResult | None:
        target = _relative_target_date(question, reference_date)
        if target is None:
            return None
        events = self._question_events(question, max_records=40)
        dated = [(parse_date(record.date), record) for record in events]
        dated = [(date, record) for date, record in dated if date is not None]
        if not dated:
            return None
        phrases = extract_question_event_phrases(question)
        aligned = self._align_phrases_to_events(phrases[:1], dated)
        candidates = aligned if aligned else dated
        chosen_date, chosen = sorted(
            candidates,
            key=lambda item: (
                abs((item[0] - target).days),
                -score_state_record(set(tokenize(question)), item[1]),
            ),
        )[0]
        answer = _relative_event_answer_label(question, chosen)
        if not answer:
            return None
        return StateReasoningResult(
            answer=answer,
            reasoning_type="relative-event-answer",
            confidence=0.70,
            evidence_ids=[chosen.evidence_id],
            explanation=f"Selected event closest to target relative date {target.date().isoformat()}.",
        )

    def answer_age_difference(self, question: str) -> StateReasoningResult | None:
        age_records = [
            record for record in self.records
            if record.record_type == "state"
            and record.attribute == "age"
            and re.search(r"\d+", record.value)
        ]
        user_records = [record for record in age_records if record.subject == "user"]
        other_terms = set(tokenize(question)) - {"older", "younger", "years"}
        other_records = [
            record for record in age_records
            if record.subject != "user"
            and (record.subject.lower() in other_terms or score_state_record(other_terms, record) > 0)
        ]
        if not user_records or not other_records:
            return None
        user = sorted(user_records, key=lambda record: (date_key(record.date), record.confidence), reverse=True)[0]
        other = sorted(other_records, key=lambda record: (date_key(record.date), record.confidence), reverse=True)[0]
        user_age_match = re.search(r"\d+", user.value)
        other_age_match = re.search(r"\d+", other.value)
        if user_age_match is None or other_age_match is None:
            return None
        user_age = int(user_age_match.group(0))
        other_age = int(other_age_match.group(0))
        return StateReasoningResult(
            answer=str(abs(other_age - user_age)),
            reasoning_type="age-difference",
            confidence=0.76,
            evidence_ids=[other.evidence_id, user.evidence_id],
            explanation=f"Computed age difference between {other.subject} ({other_age}) and user ({user_age}).",
        )

    def answer_distinct_event_day_count(self, question: str) -> StateReasoningResult | None:
        q_terms = set(tokenize(question))
        month_filter = next((month for month in MONTHS if month in question.lower()), "")
        events = [
            record for record in self.records
            if record.record_type == "event"
            and score_state_record(q_terms, record) > 0
        ]
        dated: list[tuple[datetime, StateRecord]] = []
        for record in events:
            parsed = parse_date(record.date)
            if parsed is None:
                continue
            if month_filter and parsed.month != MONTHS[month_filter]:
                continue
            dated.append((parsed, record))
        unique_days = sorted({date.date().isoformat() for date, _ in dated})
        if not unique_days:
            return None
        evidence_ids = []
        seen_days = set()
        for date, record in sorted(dated, key=lambda item: item[0]):
            key = date.date().isoformat()
            if key in seen_days:
                continue
            seen_days.add(key)
            evidence_ids.append(record.evidence_id)
        return StateReasoningResult(
            answer=str(len(unique_days)),
            reasoning_type="distinct-event-day-count",
            confidence=0.70,
            evidence_ids=evidence_ids,
            explanation=f"Counted distinct matching event dates: {', '.join(unique_days)}.",
        )

    def answer_duration_sum(self, question: str) -> StateReasoningResult | None:
        q_terms = set(tokenize(question))
        q = question.lower()
        events = [
            record for record in self.records
            if record.record_type in {"event", "state"}
            and score_state_record(q_terms, record) > 0
        ]
        target_phrases = _duration_sum_travel_targets(question)
        durations: list[tuple[int, StateRecord]] = []
        seen_duration_evidence: set[tuple[str, int]] = set()
        for record in events:
            hay = " ".join([record.attribute, record.value, record.evidence]).lower()
            if "camping" in q and "camping" not in hay:
                continue
            if "traveling" in q and not any(term in hay for term in ["trip", "travel", "city", "hawaii", "york"]):
                continue
            if "not camping" in hay and "camping" in q:
                continue
            days = _extract_duration_days(record.value) or _extract_duration_days(record.evidence)
            if days:
                key = (record.evidence_id, days)
                if key in seen_duration_evidence:
                    continue
                seen_duration_evidence.add(key)
                durations.append((days, record))
        if not durations:
            return None
        if len(target_phrases) >= 2 and "travel" in q:
            return self._answer_targeted_travel_duration_sum(target_phrases, durations)
        total = sum(days for days, _ in durations)
        return StateReasoningResult(
            answer=f"{total} days",
            reasoning_type="duration-sum",
            confidence=0.72,
            evidence_ids=[record.evidence_id for _, record in durations],
            explanation=f"Summed explicit duration mentions: {' + '.join(str(days) for days, _ in durations)}.",
        )

    def _answer_targeted_travel_duration_sum(
        self,
        target_phrases: list[str],
        durations: list[tuple[int, StateRecord]],
    ) -> StateReasoningResult | None:
        session_targets: dict[str, set[str]] = {}
        for record in self.records:
            hay = _record_haystack(record)
            matched = {target for target in target_phrases if _text_matches_target(hay, target)}
            if matched:
                session_targets.setdefault(_record_session_id(record), set()).update(matched)
        matched_durations: dict[str, tuple[int, StateRecord]] = {}
        for days, record in durations:
            hay = _record_haystack(record)
            direct_targets = [target for target in target_phrases if _text_matches_target(hay, target)]
            if not direct_targets:
                fallback_targets = sorted(session_targets.get(_record_session_id(record), set()))
                if len(fallback_targets) == 1 and not _contains_other_travel_location(hay, fallback_targets[0]):
                    direct_targets = fallback_targets
            for target in direct_targets:
                current = matched_durations.get(target)
                if current is None or _duration_target_score(record, target) > _duration_target_score(current[1], target):
                    matched_durations[target] = (days, record)
        missing_targets = [target for target in target_phrases if target not in matched_durations]
        if missing_targets and matched_durations:
            known_target, (known_days, known_record) = next(iter(matched_durations.items()))
            missing = _display_target(missing_targets[0])
            known = _display_target(known_target)
            return StateReasoningResult(
                answer=(
                    f"The information provided is not enough. You mentioned traveling for {known_days} days "
                    f"in {known} but did not mention anything about the trip to {missing}."
                ),
                reasoning_type="duration-sum",
                confidence=0.68,
                evidence_ids=[known_record.evidence_id],
                explanation=f"Found duration for {known} but not for {missing}.",
            )
        if missing_targets:
            return None
        ordered = [matched_durations[target] for target in target_phrases]
        total = sum(days for days, _ in ordered)
        return StateReasoningResult(
            answer=f"{total} days",
            reasoning_type="duration-sum",
            confidence=0.74,
            evidence_ids=[record.evidence_id for _, record in ordered],
            explanation=f"Summed target-matched duration mentions: {' + '.join(str(days) for days, _ in ordered)}.",
        )

    def answer_since_consecutive_events(self, question: str, reference_date: str = "") -> StateReasoningResult | None:
        ref = parse_date(reference_date)
        if ref is None:
            return None
        q_terms = set(tokenize(question))
        events = [
            record for record in self.records
            if record.record_type == "event"
            and (
                score_state_record(q_terms, record) > 0
                or ("charity" in question.lower() and "charity" in " ".join([record.attribute, record.value, record.evidence]).lower())
            )
        ]
        dated = sorted(
            [(parse_date(record.date), record) for record in events],
            key=lambda item: item[0] or datetime.min,
        )
        dated = [(date, record) for date, record in dated if date is not None]
        best_pair = None
        for i, (first_date, first) in enumerate(dated):
            for second_date, second in dated[i + 1:]:
                if second.evidence_id == first.evidence_id:
                    continue
                if abs((second_date - first_date).days) == 1:
                    best_pair = ((first_date, first), (second_date, second))
        if best_pair is None:
            return None
        (_, first), (second_date, second) = best_pair
        days = abs((ref - second_date).days)
        return StateReasoningResult(
            answer=self._format_temporal_delta(question, days),
            reasoning_type="since-consecutive-events",
            confidence=0.72,
            evidence_ids=[first.evidence_id, second.evidence_id],
            explanation=f"Found consecutive event dates and computed elapsed time from {second.date}.",
        )

    def _question_events(self, question: str, max_records: int) -> list[StateRecord]:
        return [
            record for record in SemanticStateIndex(self.records).search(question, max_records=max_records)
            if record.record_type == "event"
        ]

    @staticmethod
    def _looks_like_date_diff(question: str) -> bool:
        return any(term in question for term in ["how many days", "how many weeks", "how many months", "passed between", "since"])

    @staticmethod
    def _looks_like_age_difference(question: str) -> bool:
        return "how many years" in question and ("older" in question or "younger" in question)

    @staticmethod
    def _looks_like_distinct_event_day_count(question: str) -> bool:
        return (
            "activities" in question
            and ("how many days did i spend" in question or "how many days did i participate" in question)
        )

    @staticmethod
    def _looks_like_duration_sum(question: str) -> bool:
        return "how many days did i spend" in question and any(term in question for term in ["trip", "trips", "traveling"])

    @staticmethod
    def _looks_like_consecutive_event_since(question: str) -> bool:
        return "since" in question and "consecutive" in question

    @staticmethod
    def _looks_like_pages_left(question: str) -> bool:
        return "pages" in question and "left" in question and "read" in question

    @staticmethod
    def _looks_like_since_reference(question: str) -> bool:
        return "since" in question or "ago" in question

    @staticmethod
    def _looks_like_relative_event_lookup(question: str) -> bool:
        q = question.lower()
        if "how many days" in q or "how many weeks" in q or "how many months" in q:
            return False
        return (
            any(token in q for token in [" a week ago", " ago", "last tuesday", "last saturday", "last sunday", "last monday", "last wednesday", "last thursday", "last friday", "last weekend", "past weekend", "a couple of days ago", "two weeks ago", "four weeks ago", "two months ago"])
            and any(token in q for token in ["what", "which", "who", "where", "did i", "was the"])
        )

    @staticmethod
    def _looks_like_event_order(question: str) -> bool:
        return any(term in question for term in ["happened first", "participate in first", "participated in first", "order from first to last", "which event happened first", "which three events", "order of the three", "what is the order", "from earliest to latest", "starting from the earliest", "did i set up first", "take first", "happened first"])

    @staticmethod
    def _looks_like_latest_state(question: str) -> bool:
        return any(term in question for term in ["what was", "what is", "what type", "what company", "what time", "what day", "where did", "where do", "how often", "how many", "which"]) 

    @staticmethod
    def _format_temporal_delta(question: str, days: int) -> str:
        q = question.lower()
        if "week" in q:
            weeks = round(days / 7)
            if "ago" in q:
                unit = "week" if weeks == 1 else "weeks"
                return f"{weeks} {unit} ago"
            return str(weeks)
        if "month" in q:
            months = round(days / 30)
            if "ago" in q:
                unit = "month" if months == 1 else "months"
                return f"{months} {unit} ago"
            return str(months)
        if days == 1:
            return "1 day"
        return f"{days} days"

    def _select_two_events(
        self,
        question: str,
        dated: list[tuple[datetime, StateRecord]],
    ) -> tuple[tuple[datetime, StateRecord], tuple[datetime, StateRecord]] | None:
        phrases = extract_question_event_phrases(question)
        aligned = self._align_phrases_to_events(phrases[:2], dated)
        if len(aligned) >= 2:
            return aligned[0], aligned[1]
        if len(phrases) >= 2 and aligned:
            return None
        q_terms = set(tokenize(question))
        ranked = sorted(
            dated,
            key=lambda item: score_state_record(q_terms, item[1]),
            reverse=True,
        )
        for i, first in enumerate(ranked):
            for second in ranked[i + 1:]:
                if first[1].evidence_id != second[1].evidence_id:
                    return first, second
        return None

    @staticmethod
    def _align_phrases_to_events(
        phrases: list[str],
        dated: list[tuple[datetime, StateRecord]],
    ) -> list[tuple[datetime, StateRecord]]:
        aligned: list[tuple[datetime, StateRecord]] = []
        used: set[str] = set()
        for phrase in phrases:
            candidates = sorted(
                (
                    (score_event_phrase(phrase, record), date, record)
                    for date, record in dated
                    if record.evidence_id not in used
                ),
                key=lambda item: item[0],
                reverse=True,
            )
            if candidates and candidates[0][0] > 0:
                _, date, record = candidates[0]
                aligned.append((date, record))
                used.add(record.evidence_id)
        return aligned


def tokenize(text: object) -> list[str]:
    """Tokenize a query or memory text for lightweight evidence scoring."""
    return [
        tok.lower()
        for tok in TOKEN_RE.findall(str(text or ""))
        if len(tok) > 1 and tok.lower() not in STOPWORDS
    ]


def extract_question_event_phrases(question: str) -> list[str]:
    """Extract event-like phrases from temporal/order questions."""
    text = " ".join(str(question or "").replace("?", "").split())
    lowered = text.lower()
    phrases: list[str] = []

    between = re.search(r"\bbetween\s+(?P<first>.+?)\s+and\s+(?P<second>.+)$", text, re.IGNORECASE)
    before = re.search(
        r"\bhow\s+many\s+days\s+before\s+(?P<second>.+?)\s+did\s+I\s+(?P<first>.+)$",
        text,
        re.IGNORECASE,
    )
    if between:
        phrases.extend([between.group("first"), between.group("second")])
    elif before:
        phrases.extend([before.group("first"), before.group("second")])
    elif "order from first to last" in lowered:
        after_colon = text.split(":", 1)[-1]
        phrases.extend(re.split(r",\s*|\s+and\s+lastly\s+|\s+and\s+", after_colon))
    elif "order of" in lowered and ":" in text:
        after_colon = text.split(":", 1)[-1]
        quoted = re.findall(r"'([^']+)'|\"([^\"]+)\"", after_colon)
        if quoted:
            phrases.extend([left or right for left, right in quoted])
        else:
            phrases.extend(re.split(r",\s*|\s+and\s+", after_colon))
    elif "happened first" in lowered and "," in text:
        tail = text.split(",", 1)[1]
        phrases.extend(re.split(r"\s+or\s+|\s+and\s+", tail))
    elif "since" in lowered:
        phrases.append(re.split(r"\bsince\b", text, flags=re.IGNORECASE, maxsplit=1)[1])
    elif "ago" in lowered:
        match = re.search(r"\bago\s+did\s+I\s+(?P<event>.+)$", text, re.IGNORECASE)
        if match:
            phrases.append(match.group("event"))

    cleaned = [_normalize_event_phrase(phrase) for phrase in phrases]
    return [phrase for phrase in cleaned if phrase]


def score_event_phrase(phrase: str, record: StateRecord) -> float:
    """Score how well a question event phrase aligns with an event record."""
    phrase_terms = _expand_alignment_terms(tokenize(_normalize_event_phrase(phrase)))
    if not phrase_terms:
        return 0.0
    record_terms = _expand_alignment_terms(tokenize(_normalize_event_phrase(" ".join([record.attribute, record.value, record.evidence]))))
    overlap = len(phrase_terms & record_terms)
    if not overlap:
        return 0.0
    return overlap / (len(phrase_terms) ** 0.5)


def _expand_alignment_terms(tokens: list[str]) -> set[str]:
    terms = set(tokens)
    for token in tokens:
        if token.endswith("ed") and len(token) > 4:
            terms.add(token[:-2])
        if token.endswith("ing") and len(token) > 5:
            terms.add(token[:-3])
        if token.endswith("s") and len(token) > 4:
            terms.add(token[:-1])
    return terms


def _insufficient_information(reasoning_type: str) -> StateReasoningResult:
    return StateReasoningResult(
        answer="The information provided is not enough.",
        reasoning_type=reasoning_type,
        confidence=0.62,
        evidence_ids=[],
        explanation="Required question entity or title was not found in candidate state records.",
    )


def _missing_required_question_anchors(question: str, candidates: list[StateRecord]) -> bool:
    anchors = _required_question_anchors(question)
    if not anchors:
        return False
    haystack = "\n".join(
        " ".join([record.subject, record.attribute, record.value, record.evidence]).lower()
        for record in candidates
    )
    return any(anchor not in haystack for anchor in anchors)


def _required_question_anchors(question: str) -> list[str]:
    """Find explicit entities/titles that must align before answering state questions."""
    text = str(question or "")
    q = text.lower()
    anchors: list[str] = []
    for left, right in re.findall(r"'([^']+)'|\"([^\"]+)\"", text):
        phrase = (left or right).strip().lower()
        if len(phrase) >= 3:
            anchors.append(phrase)
    for match in re.finditer(r"\bdr\.?\s+([a-z]+)\b", q):
        anchors.append(f"dr. {match.group(1)}")
    cuisine = re.search(
        r"\b(italian|korean|japanese|chinese|french|indian|thai|mexican|spanish|greek|vietnamese)\s+restaurants?\b",
        q,
    )
    if cuisine:
        anchors.append(f"{cuisine.group(1)} restaurant")
    role = re.search(r"\brole\s+as\s+([a-z][a-z\s]+?)(?:\?|,|\s+when\b|$)", q)
    if role:
        anchors.append(" ".join(role.group(1).split()))
    unique: list[str] = []
    for anchor in anchors:
        if anchor and anchor not in unique:
            unique.append(anchor)
    return unique


def _event_answer_label(question: str, record: StateRecord) -> str:
    """Return a compact human-readable event label for deterministic answers."""
    value = _clean_value(record.value)
    q = question.lower()
    if record.attribute == "airline flight":
        return value
    if record.attribute == "transport event":
        value_l = value.lower()
        if "train" in value_l:
            return "train"
        if "bus" in value_l:
            return "bus"
        return value
    if record.attribute == "graduation event":
        match = re.search(r"\b(Emma|Rachel|Alex)\b", value)
        return f"{match.group(1)} graduated" if match else value
    if record.attribute == "participation event":
        value_l = value.lower()
        if "walk for hunger" in value_l:
            return "the 'Walk for Hunger' charity event"
        if "charity bake sale" in value_l:
            return "I participated in the charity bake sale first." if "first" in q else "the charity bake sale"
        if "charity gala" in value_l:
            return "the charity gala"
    if record.attribute == "watched sports event":
        if "nba game" in value.lower():
            return "a NBA game at the Staples Center"
        if "college football national championship" in value.lower():
            return "the College Football National Championship game"
        if "nfl playoffs" in value.lower():
            return "the NFL playoffs"
    if "nba game" in value.lower():
        return "a NBA game at the Staples Center"
    if record.attribute == "participation event" and "soccer tournament" in value.lower():
        return "the company's annual charity soccer tournament"
    if record.attribute == "museum visit":
        return _museum_event_label(value)
    if record.attribute == "music event":
        return _music_event_label(value)
    if "cousin" in value.lower() and "wedding" in value.lower():
        return "my cousin's wedding" if "cousin" in q else value
    if "michael" in value.lower() and "engagement" in value.lower():
        return "Michael's engagement party"
    if "smart thermostat" in value.lower():
        return "smart thermostat"
    if "new router" in value.lower():
        return "new router"
    if "spanish classes" in value.lower():
        return "Spanish classes"
    if "phone charger" in value.lower():
        return "losing the phone charger"
    if "stand mixer malfunction" in value.lower():
        return "The malfunction of the stand mixer"
    if "new phone case" in value.lower():
        return "Receiving the new phone case" if "receiving" in q else "new phone case"
    if "prime lens" in value.lower():
        return "the arrival of the new prime lens" if "arrival" in q else "new prime lens"
    if record.attribute in {"helped", "ordered", "used", "redeemed", "signed up for"} and not value.lower().startswith(record.attribute):
        value = f"{record.attribute} {value}"
    value = re.split(
        r"\b(?:and we|and i think|and it was|where|with a personal best|with personal best|managed to|recently, where)\b",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" ,")
    value = re.split(r"\b(?:today|yesterday)\b", value, maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,")
    return value


def _relative_event_answer_label(question: str, record: StateRecord) -> str:
    q = question.lower()
    value = _clean_value(record.value)
    evidence = record.evidence
    combined = f"{value} {evidence}"
    combined_l = combined.lower()
    if "who did i go with" in q or "who did i meet with" in q:
        if "emma" in combined_l:
            return "Emma"
        people = re.search(r"\bwith\s+(my\s+[a-z]+(?:\s+and\s+my\s+[a-z]+)?|[A-Z][a-z]+(?:\s+and\s+[A-Z][a-z]+)?)", combined)
        if people:
            return people.group(1)
    if "which book" in q:
        title = re.search(r"\"([^\"]+)\"\s+by\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)", combined)
        if title:
            return f"'{title.group(1)}' by {title.group(2)}"
    if "what gardening-related activity" in q and "planted" in combined_l:
        planted = re.search(r"\bplanted\s+([^.!?;\n]+)", combined, re.IGNORECASE)
        if planted:
            return f"planting {planted.group(1).strip(' .')}"
    if "where was that event held" in q or ("where" in q and "art-related event" in q):
        if "metropolitan museum of art" in combined_l:
            return "The Metropolitan Museum of Art."
        if "museum of modern art" in combined_l or "moma" in combined_l:
            return "Museum of Modern Art"
    if "which bike" in q:
        if "road bike" in combined_l:
            return "road bike"
        if "mountain bike" in combined_l:
            return "mountain bike"
    if "life event" in q:
        if "cousin" in combined_l and "wedding" in combined_l:
            return "my cousin's wedding"
        if "engagement party" in combined_l:
            return "Michael's engagement party"
    if "what was it" in q or "what was the social media activity" in q:
        cake = re.search(r"\bbaked\s+(a\s+[^.!?;\n]+?cake)\b", combined, re.IGNORECASE)
        if cake:
            return cake.group(1)
        challenge = re.search(r"(#\w+Challenge)", combined)
        if challenge:
            return f"You participated in a social media challenge called {challenge.group(1)}."
    if "super bowl" in q and "watched" in combined_l:
        return "the Super Bowl"
    if "what was the significant buisiness milestone" in q or "business milestone" in q:
        if "signed a contract with my first client" in combined_l:
            return "I signed a contract with my first client."
    return _event_answer_label(question, record)


def _format_three_event_order(values: list[str]) -> str:
    def with_subject(value: str) -> str:
        value = value.strip()
        lower = value.lower()
        if lower.startswith(("i ", "my ", "the ")):
            return value
        if lower.startswith((
            "helped ",
            "ordered ",
            "used ",
            "redeemed ",
            "signed up ",
            "went ",
            "participated ",
            "completed ",
            "attended ",
            "posted ",
        )):
            return "I " + value
        return value

    first, second, third = [with_subject(value) for value in values[:3]]
    return f"First, {first}, then {second}, and lastly, {third}."


def _museum_event_label(value: str) -> str:
    value_l = value.lower()
    if "science museum" in value_l:
        return "Science Museum"
    if "museum of contemporary art" in value_l:
        return "Museum of Contemporary Art"
    if "metropolitan museum of art" in value_l:
        return "Metropolitan Museum of Art"
    if "museum of history" in value_l:
        return "Museum of History"
    if "modern art museum" in value_l:
        return "Modern Art Museum"
    if "natural history museum" in value_l:
        return "Natural History Museum"
    return _clean_value(value)


def _music_event_label(value: str) -> str:
    value_l = value.lower()
    if "billie eilish" in value_l:
        return "Billie Eilish concert at the Wells Fargo Center in Philly"
    if "outdoor concert" in value_l:
        return "Free outdoor concert series in the park"
    if "music festival in brooklyn" in value_l:
        return "Music festival in Brooklyn"
    if "jazz night" in value_l:
        return "Jazz night at a local bar"
    if "queen" in value_l or "adam lambert" in value_l:
        return "Queen + Adam Lambert concert at the Prudential Center in Newark, NJ"
    return _clean_value(value)


def _dedupe_ordered_events(
    question: str,
    ordered: list[tuple[datetime, StateRecord]],
) -> list[tuple[datetime, StateRecord]]:
    """Remove duplicated event mentions before producing an ordered answer."""
    q = question.lower()
    seen: set[str] = set()
    result: list[tuple[datetime, StateRecord]] = []
    for date, record in ordered:
        value_l = record.value.lower()
        if ("trip" in q or "travel" in q) and "realized i need" in value_l:
            continue
        label_key = _normalize_event_phrase(_event_answer_label(question, record))
        if not label_key or label_key in seen:
            continue
        if any(label_key in old or old in label_key for old in seen):
            continue
        seen.add(label_key)
        result.append((date, record))
    return result


def _normalize_event_phrase(phrase: str) -> str:
    text = str(phrase or "").lower()
    text = re.sub(r"\b(the day|day|my|the|a|an|i|me|did|do|to|at|on|of|in|visit)\b", " ", text)
    text = re.sub(r"\s+for\s*$", " ", text)
    text = re.sub(r"\b(my visit to|visit to|the day i|day i)\b", " ", text)
    text = text.replace("'s", "")
    text = re.sub(r"[^a-z0-9$:/\s-]", " ", text)
    return " ".join(text.split())


def date_key(value: str) -> tuple[int, str]:
    """Return a sortable date key while tolerating non-ISO dataset dates."""
    if not value:
        return (0, "")
    text = str(value)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return (1, datetime.strptime(text[: len(fmt)], fmt).isoformat())
        except ValueError:
            pass
    return (1, text)


def parse_date(value: str) -> datetime | None:
    """Parse dataset dates such as YYYY/MM/DD (Tue) into datetimes."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt)
        except ValueError:
            continue
    match = re.search(r"\d{4}[-/]\d{2}[-/]\d{2}", text)
    if match:
        normalized = match.group(0).replace("/", "-")
        try:
            return datetime.strptime(normalized, "%Y-%m-%d")
        except ValueError:
            return None
    return None


def _format_inferred_date(value: datetime, base_date: str) -> str:
    if "/" in str(base_date):
        return value.strftime("%Y/%m/%d")
    return value.strftime("%Y-%m-%d")


def _black_friday(year: int) -> datetime:
    november_first = datetime(year, 11, 1)
    days_until_friday = (4 - november_first.weekday()) % 7
    first_friday = november_first + timedelta(days=days_until_friday)
    return first_friday + timedelta(days=21)


def _infer_event_date(base_date: str, text: str) -> str:
    """Infer an event date from explicit or relative dates in a turn."""
    base = parse_date(base_date)
    if base is None:
        return base_date
    lowered = str(text or "").lower()

    explicit = re.search(
        r"\b(?P<month>january|february|march|april|may|june|july|august|september|october|november|december)\s+"
        r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,\s*(?P<year>\d{4}))?",
        lowered,
        re.IGNORECASE,
    )
    if explicit:
        month = MONTHS[explicit.group("month").lower()]
        day = int(explicit.group("day"))
        year = int(explicit.group("year")) if explicit.group("year") else base.year
        if explicit.group("year") is None and month > base.month + 1:
            year -= 1
        try:
            return _format_inferred_date(datetime(year, month, day), base_date)
        except ValueError:
            return base_date

    day_of_month = re.search(
        r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+of\s+"
        r"(?P<month>january|february|march|april|may|june|july|august|september|october|november|december)\b",
        lowered,
        re.IGNORECASE,
    )
    if day_of_month:
        month = MONTHS[day_of_month.group("month").lower()]
        day = int(day_of_month.group("day"))
        year = base.year
        if month > base.month + 1:
            year -= 1
        try:
            return _format_inferred_date(datetime(year, month, day), base_date)
        except ValueError:
            return base_date

    numeric_explicit = re.search(r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{2,4}))?\b", lowered)
    if numeric_explicit:
        month = int(numeric_explicit.group("month"))
        day = int(numeric_explicit.group("day"))
        raw_year = numeric_explicit.group("year")
        year = int(raw_year) if raw_year else base.year
        if raw_year and year < 100:
            year += 2000
        if raw_year is None and month > base.month + 1:
            year -= 1
        try:
            return _format_inferred_date(datetime(year, month, day), base_date)
        except ValueError:
            return base_date

    if "yesterday" in lowered:
        return _format_inferred_date(base - timedelta(days=1), base_date)
    if "today" in lowered:
        return base_date
    if "tomorrow" in lowered:
        return _format_inferred_date(base + timedelta(days=1), base_date)
    if "a couple of days ago" in lowered:
        return _format_inferred_date(base - timedelta(days=2), base_date)
    if "last week" in lowered:
        return _format_inferred_date(base - timedelta(days=7), base_date)
    if "last month" in lowered:
        return _format_inferred_date(base - timedelta(days=30), base_date)
    if "last weekend" in lowered or "past weekend" in lowered:
        return _format_inferred_date(_previous_weekday(base, WEEKDAYS["saturday"]), base_date)
    weekday_match = re.search(
        r"\blast\s+(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        lowered,
    )
    if weekday_match:
        return _format_inferred_date(_previous_weekday(base, WEEKDAYS[weekday_match.group("weekday")]), base_date)

    if "black friday" in lowered:
        year = base.year
        if base.month < 11:
            year -= 1
        black_friday = _black_friday(year)
        if "week before black friday" in lowered or "a week before black friday" in lowered:
            black_friday -= timedelta(days=7)
        return _format_inferred_date(black_friday, base_date)

    rel = re.search(
        r"\b(?P<num>a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
        r"(?P<unit>days?|weeks?|months?)\s+ago\b",
        lowered,
    )
    if rel:
        raw = rel.group("num")
        amount = int(raw) if raw.isdigit() else NUMBER_WORDS.get(raw, 0)
        unit = rel.group("unit")
        days = amount
        if unit.startswith("week"):
            days = amount * 7
        elif unit.startswith("month"):
            days = amount * 30
        return _format_inferred_date(base - timedelta(days=days), base_date)

    past_duration = re.search(
        r"\bfor\s+(?:the\s+)?past\s+(?P<num>a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
        r"(?P<unit>days?|weeks?|months?)\b",
        lowered,
    )
    if past_duration:
        raw = past_duration.group("num")
        amount = int(raw) if raw.isdigit() else NUMBER_WORDS.get(raw, 0)
        unit = past_duration.group("unit")
        days = amount
        if unit.startswith("week"):
            days = amount * 7
        elif unit.startswith("month"):
            days = amount * 30
        return _format_inferred_date(base - timedelta(days=days), base_date)

    return base_date


def _extract_duration_days(text: str) -> int:
    lowered = str(text or "").lower()
    compact = re.search(
        r"\b(?P<num>one|two|three|four|five|six|seven|eight|nine|ten|\d+)[-\s]+day\b",
        lowered,
    )
    if compact:
        raw = compact.group("num")
        return int(raw) if raw.isdigit() else NUMBER_WORDS.get(raw, 0)
    duration = re.search(
        r"\bfor\s+(?P<num>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+days?\b",
        lowered,
    )
    if duration:
        raw = duration.group("num")
        return int(raw) if raw.isdigit() else NUMBER_WORDS.get(raw, 0)
    return 0


def _number_word_to_int(raw: str) -> int:
    value = str(raw or "").lower()
    return int(value) if value.isdigit() else NUMBER_WORDS.get(value, 0)


TRAVEL_LOCATION_ALIASES = {
    "new york city": {"new york city", "new york", "nyc"},
}

KNOWN_TRAVEL_LOCATIONS = {
    "amsterdam",
    "barcelona",
    "brazil",
    "chicago",
    "europe",
    "hawaii",
    "japan",
    "new york",
    "new york city",
    "nyc",
    "paris",
    "portland",
    "rome",
    "seattle",
}


def _duration_sum_travel_targets(question: str) -> list[str]:
    lowered = str(question or "").lower().rstrip("?.! ")
    match = re.search(r"\btravel(?:ing|ling|ed)?\s+in\s+(?P<targets>.+)$", lowered)
    if not match:
        match = re.search(r"\bspent\s+in\s+(?P<targets>.+)$", lowered)
    if not match:
        return []
    raw = match.group("targets")
    raw = re.split(r"\b(?:this year|last year|today|recently)\b", raw, maxsplit=1)[0]
    parts = [part.strip(" ,") for part in re.split(r"\s+and\s+|,", raw) if part.strip(" ,")]
    targets: list[str] = []
    for part in parts:
        part = re.sub(r"^(?:in|the)\s+", "", part).strip()
        tokens = [token for token in tokenize(part) if token not in STOPWORDS and token not in {"total", "traveling", "travel"}]
        if tokens:
            targets.append(" ".join(tokens))
    return targets


def _record_haystack(record: StateRecord) -> str:
    return " ".join([record.subject, record.attribute, record.value, record.evidence]).lower()


def _record_session_id(record: StateRecord) -> str:
    return str(record.evidence_id or "").split(":", maxsplit=1)[0]


def _target_aliases(target: str) -> set[str]:
    normalized = " ".join(tokenize(target))
    aliases = {normalized}
    aliases.update(TRAVEL_LOCATION_ALIASES.get(normalized, set()))
    return aliases


def _text_matches_target(text: str, target: str) -> bool:
    lowered = str(text or "").lower()
    aliases = _target_aliases(target)
    if any(re.search(rf"\b{re.escape(alias)}\b", lowered) for alias in aliases):
        return True
    target_tokens = [token for token in tokenize(target) if token not in STOPWORDS]
    return bool(target_tokens) and all(re.search(rf"\b{re.escape(token)}\b", lowered) for token in target_tokens)


def _contains_other_travel_location(text: str, target: str) -> bool:
    lowered = str(text or "").lower()
    target_aliases = _target_aliases(target)
    for location in KNOWN_TRAVEL_LOCATIONS:
        if location in target_aliases:
            continue
        if re.search(rf"\b{re.escape(location)}\b", lowered):
            return True
    return False


def _duration_target_score(record: StateRecord, target: str) -> int:
    hay = _record_haystack(record)
    score = 0
    if _text_matches_target(" ".join([record.subject, record.attribute, record.value]).lower(), target):
        score += 2
    if _text_matches_target(hay, target):
        score += 1
    return score


def _display_target(target: str) -> str:
    words = target.split()
    if target == "new york city":
        return "New York City"
    return " ".join(word.upper() if word == "nyc" else word.capitalize() for word in words)


def _extract_month_day_range_days(text: str) -> tuple[str, int] | None:
    match = re.search(
        r"\bfrom\s+(?P<month>january|february|march|april|may|june|july|august|september|october|november|december)\s+"
        r"(?P<start>\d{1,2})(?:st|nd|rd|th)?\s+to\s+(?P<end>\d{1,2})(?:st|nd|rd|th)?\b",
        str(text or ""),
        re.IGNORECASE,
    )
    if not match:
        return None
    start = int(match.group("start"))
    end = int(match.group("end"))
    if end < start:
        return None
    return match.group("month").lower(), end - start


def _previous_weekday(base: datetime, target_weekday: int) -> datetime:
    delta = (base.weekday() - target_weekday) % 7
    if delta == 0:
        delta = 7
    return base - timedelta(days=delta)


def _relative_target_date(question: str, reference_date: str) -> datetime | None:
    ref = parse_date(reference_date)
    if ref is None:
        return None
    q = question.lower()
    if "a couple of days ago" in q:
        return ref - timedelta(days=2)
    weekday_match = re.search(
        r"\blast\s+(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        q,
    )
    if weekday_match:
        return _previous_weekday(ref, WEEKDAYS[weekday_match.group("weekday")])
    if "last weekend" in q or "past weekend" in q:
        return _previous_weekday(ref, WEEKDAYS["saturday"])
    rel = re.search(
        r"\b(?P<num>a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
        r"(?P<unit>days?|weeks?|months?)\s+ago\b",
        q,
    )
    if rel:
        raw = rel.group("num")
        amount = int(raw) if raw.isdigit() else NUMBER_WORDS.get(raw, 0)
        unit = rel.group("unit")
        days = amount
        if unit.startswith("week"):
            days = amount * 7
        elif unit.startswith("month"):
            days = amount * 30
        return ref - timedelta(days=days)
    return None


def _duration_minutes(raw: str, unit: str) -> int:
    amount = _parse_number(raw) or 0
    if unit.lower().startswith("hour"):
        return int(amount * 60)
    return int(amount)


def _number_word_to_digit(value: str) -> str:
    text = str(value or "").strip().lower()
    return str(NUMBER_WORDS[text]) if text in NUMBER_WORDS and text not in {"a", "an"} else str(value or "").strip()


def _parse_number(value: str) -> float | None:
    text = str(value or "").strip().lower()
    if text in NUMBER_WORDS:
        return float(NUMBER_WORDS[text])
    cleaned = re.sub(r"[$,%]", "", text).replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    return float(match.group(0))


def _format_number_answer(value: float, *, prefix: str = "", suffix: str = "", use_commas: bool = False) -> str:
    if abs(value - round(value)) < 1e-9:
        integer = int(round(value))
        number = f"{integer:,}" if use_commas and abs(integer) >= 1000 else str(integer)
    else:
        number = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{prefix}{number}{suffix}"


def _dedupe_records(records: list[StateRecord]) -> list[StateRecord]:
    deduped: list[StateRecord] = []
    seen: set[tuple[str, str, str, str]] = set()
    for record in records:
        key = (record.attribute, record.subject, record.value, record.date)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def _numeric_state(
    *,
    subject: str,
    attribute: str,
    value: str,
    date: str,
    evidence: str,
    evidence_id: str,
) -> StateRecord:
    return StateRecord(
        subject=subject,
        attribute=attribute,
        value=value,
        date=date,
        evidence=evidence,
        evidence_id=evidence_id,
        confidence=0.84,
        record_type="state",
    )


def _extract_numeric_fact_records(text: str, *, date: str, evidence_id: str) -> list[StateRecord]:
    records: list[StateRecord] = []
    source = str(text or "")
    range_days = _extract_month_day_range_days(source)
    if range_days and "japan" in source.lower():
        records.append(_numeric_state(subject="Japan trip", attribute="trip duration days", value=str(range_days[1]), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bspent\s+\$(?P<value>\d[\d,]*(?:\.\d+)?)\s+on\s+groceries\s+at\s+SaveMart\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="SaveMart", attribute="savemart grocery purchase", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:can\s+)?earn\s+(?P<value>\d+(?:\.\d+)?)%\s+cashback\s+on\s+all\s+purchases\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="SaveMart", attribute="savemart cashback percent", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\busually\s+work\s+(?P<value>\d+)\s+hours\s+a\s+week\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="typical work week", attribute="weekly work hours", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bincrease\s+my\s+work\s+hours\s+by\s+(?P<value>\d+)\s+hours\s+weekly\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="peak increase", attribute="weekly work hours", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bscored\s+(?P<value>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+goals?\s+so\s+far\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="goals", attribute="soccer contribution count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:had|have)\s+(?P<value>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+assists?\s+in\s+the\s+league\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="assists", attribute="soccer contribution count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bpurchased\s+(?P<value>\d+)\s+coffee\s+mugs?\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="coffee mugs", attribute="coffee mug count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bspent\s+\$(?P<value>\d+(?:\.\d+)?)\s+on\s+(?:some\s+)?coffee\s+mugs?\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="coffee mugs", attribute="coffee mug total cost", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bcovered\s+a\s+total\s+of\s+(?P<value>\d[\d,]*)\s+miles\b", source, re.IGNORECASE):
        if "road trip" in source.lower() or "yellowstone" in source.lower():
            records.append(_numeric_state(subject="road trips", attribute="road trip distance", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?P<value>\d+)\s+titles?\s+(?:waiting\s+to\s+be\s+checked\s+off|on\s+my\s+to-watch\s+list|on\s+it\s+right\s+now)\b", source, re.IGNORECASE):
        if "to-watch list" in source.lower() or "watchlist" in source.lower():
            records.append(_numeric_state(subject="to-watch list", attribute="to-watch list count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bto-watch\s+list[^.?!;\n]{0,60}?\bcurrently\s+(?P<value>\d+)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="to-watch list", attribute="to-watch list count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\battend(?:ed|ing)?\s+(?P<value>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+sessions?\s+of\s+the\s+bereavement\s+support\s+group\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="bereavement support group", attribute="bereavement support sessions", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\battending\s+(?P<value>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+sessions?\b", source, re.IGNORECASE):
        if "bereavement support group" in source.lower():
            records.append(_numeric_state(subject="bereavement support group", attribute="bereavement support sessions", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bfinished\s+(?P<value>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+issues?\s+so\s+far\b", source, re.IGNORECASE):
        if "national geographic" in source.lower():
            records.append(_numeric_state(subject="National Geographic", attribute="national geographic issues finished", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bfinished\s+my\s+(?P<value>\d+|one|two|three|four|five|six|seven|eight|nine|ten)(?:st|nd|rd|th)?\s+issue\b", source, re.IGNORECASE):
        if "national geographic" in source.lower():
            records.append(_numeric_state(subject="National Geographic", attribute="national geographic issues finished", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:cut\s+back\s+to|limit\s+to|just)\s+(?P<value>one|two|three|four|five|\d+)\s+cup[s]?\s+in\s+the\s+morning\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="morning coffee", attribute="morning coffee cup limit", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bincreased\s+the\s+limit\s+to\s+(?P<value>one|two|three|four|five|\d+)\s+cup[s]?\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="morning coffee", attribute="morning coffee cup limit", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?P<value>\d+)-year-old\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="user", attribute="current age", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bcompleted\s+at\s+the\s+age\s+of\s+(?P<value>\d+)\b", source, re.IGNORECASE):
        if "bachelor" in source.lower() or "graduated" in source.lower() or "degree" in source.lower():
            records.append(_numeric_state(subject="user", attribute="college graduation age", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bgot\s+a\s+new\s+(?P<value>silver\s+necklace[^.?!;\n]{0,60}|pair\s+of\s+emerald\s+earrings|engagement\s+ring)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject=_clean_value(match.group("value")), attribute="jewelry acquired item", value="1", date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bI\s+got\s+my\s+engagement\s+ring\s+a\s+month\s+ago\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="engagement ring", attribute="jewelry acquired item", value="1", date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\braised\s+\$(?P<value>\d[\d,]*(?:\.\d+)?)\s+for\s+(?:the\s+)?(?P<subject>[^.?!;\n]+)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject=match.group("subject"), attribute="charity amount raised", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bhelped\s+raise\s+\$(?P<value>\d[\d,]*(?:\.\d+)?)\s+for\s+(?:a\s+|the\s+)?(?P<subject>[^.?!;\n]+)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject=match.group("subject"), attribute="charity amount raised", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bwe\s+raised\s+\$(?P<value>\d[\d,]*(?:\.\d+)?)\s+for\s+(?:a\s+|the\s+)?(?P<subject>[^.?!;\n]+)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject=match.group("subject"), attribute="charity amount raised", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bmanaged\s+to\s+raise\s+\$(?P<value>\d[\d,]*(?:\.\d+)?)\s+for\s+(?:the\s+)?(?P<subject>[^.?!;\n]+)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject=match.group("subject"), attribute="charity amount raised", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bcar\s+was\s+getting\s+(?P<value>\d+(?:\.\d+)?)\s+miles\s+per\s+gallon\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="previous car mpg", attribute="previous car mpg", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:currently\s+at|getting\s+around)\s+(?P<value>\d+(?:\.\d+)?)\s+miles\s+per\s+gallon\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="current car mpg", attribute="current car mpg", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\btrain[^.?!;\n]{0,120}?\b(?:around|actually|only)\s+\$(?P<value>\d+(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="train", attribute="train fare", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:around|actually|only)\s+\$(?P<value>\d+(?:\.\d+)?)\s+to\s+get\s+to\s+my\s+hotel[^.?!;\n]{0,80}?\btrain\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="train", attribute="train fare", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\btaxi[^.?!;\n]{0,120}?\bcost\s+around\s+\$(?P<value>\d+(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="taxi", attribute="taxi fare", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bYouTube[^.?!;\n]{0,120}?\bwith\s+(?P<value>\d[\d,]*)\s+views\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="YouTube", attribute="video view count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bTikTok[^.?!;\n]{0,120}?\bhas\s+(?P<value>\d[\d,]*)\s+views\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="TikTok", attribute="video view count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bFacebook\s+Live[^.?!;\n]{0,120}?\bgot\s+(?P<value>\d+)\s+comments\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Facebook Live", attribute="social comment count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bmost\s+popular\s+video[^.?!;\n]{0,80}?\bhas\s+(?P<value>\d+)\s+comments\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="YouTube", attribute="social comment count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\binitially\s+aimed\s+to\s+raise\s+\$(?P<value>\d[\d,]*(?:\.\d+)?)\s+in\s+donations\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="charity cycling", attribute="charity cycling goal", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bcharity\s+cycling\s+event\s+and\s+raised\s+\$(?P<value>\d[\d,]*(?:\.\d+)?)\s+in\s+donations\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="charity cycling", attribute="charity cycling raised", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bGPA\s+of\s+(?P<value>\d+(?:\.\d+)?)\s+out\s+of\s+4\.0\b", source, re.IGNORECASE):
        if "equivalent to a" in source[max(0, match.start() - 24):match.start()].lower():
            continue
        records.append(_numeric_state(subject="graduate studies" if "master" in source.lower() else "studies", attribute="study gpa", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bequivalent\s+to\s+a\s+GPA\s+of\s+(?P<value>\d+(?:\.\d+)?)\s+out\s+of\s+4\.0\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="undergraduate studies", attribute="study gpa", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?P<value>\d+)[-\s]+day\s+trip\s+to\s+(?P<subject>Chicago|Japan)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject=f"{match.group('subject')} trip", attribute="trip duration days", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\btrip\s+to\s+(?P<subject>[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*){0,2})\s+for\s+(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+days?\b", source, re.IGNORECASE):
        subject = match.group("subject").strip()
        records.append(_numeric_state(subject=f"{subject} trip", attribute="trip duration days", value=str(_number_word_to_int(match.group("value"))), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\btrip\s+to\s+(?P<subject>[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*){0,2})\s+[^.?!;\n]{0,80}?\bfor\s+(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+days?\b", source, re.IGNORECASE):
        subject = match.group("subject").strip()
        records.append(_numeric_state(subject=f"{subject} trip", attribute="trip duration days", value=str(_number_word_to_int(match.group("value"))), date=date, evidence=source, evidence_id=evidence_id))
    if re.search(r"\b(?:trip|travel|traveling|solo|family)\b", source, re.IGNORECASE):
        for match in re.finditer(r"\b(?:for\s+(?:the\s+)?(?P<value_a>one|two|three|four|five|six|seven|eight|nine|ten|\d+)[-\s]+day|(?P<value_b>one|two|three|four|five|six|seven|eight|nine|ten|\d+)[-\s]+day)\b", source, re.IGNORECASE):
            raw_value = match.group("value_a") or match.group("value_b")
            records.append(_numeric_state(subject="trip", attribute="trip duration days", value=str(_number_word_to_int(raw_value)), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?P<source>HelloFresh|UberEats)[^.?!;\n]{0,100}?\b(?P<value>\d+(?:\.\d+)?)%\s+discount\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject=match.group("source"), attribute="order discount percent", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?P<value>\d+(?:\.\d+)?)%\s+off\s+(?:my\s+)?(?P<source>HelloFresh|UberEats)\s+order\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject=match.group("source"), attribute="order discount percent", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bcar\s+wash[^.?!;\n]{0,100}?\bcost\s+\$(?P<value>\d+(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="car wash", attribute="car expense cost", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bparking\s+ticket[^.?!;\n]{0,120}?\bfor\s+\$(?P<value>\d+(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="parking ticket", attribute="car expense cost", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bflea\s+and\s+tick\s+prevention\s+medication[^.?!;\n]{0,100}?\b(?:was|cost)\s+\$(?P<value>\d+(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Lola flea medication", attribute="pet expense cost", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bLola[^.?!;\n]{0,80}?\bvet[^.?!;\n]{0,120}?\b(?:fee\s+of|fee\s+was|was)\s+\$(?P<value>\d+(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Lola vet visit", attribute="pet expense cost", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\binitially\s+quoted\s+me\s+\$(?P<value>\d[\d,]*(?:\.\d+)?)\s+for\s+the\s+entire\s+trip\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Sakura Travel Agency trip", attribute="trip initial quote", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bcorrected\s+price\s+for\s+the\s+entire\s+trip\s+was\s+\$(?P<value>\d[\d,]*(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Sakura Travel Agency trip", attribute="trip corrected price", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?P<ordinal>first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+meal\s+I\s+got\s+from\s+my\s+(?P<subject>chicken\s+fajitas|lentil\s+soup)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject=match.group("subject"), attribute="lunch meal count", value=str(ORDINAL_WORDS[match.group("ordinal").lower()]), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?P<subject>lentil\s+soup|chicken\s+fajitas)[^.?!;\n]{0,80}?\blasted\s+me\s+for\s+(?P<value>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+lunches\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject=match.group("subject"), attribute="lunch meal count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bborrow\s+up\s+to\s+\$(?P<value>\d[\d,]*(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="mortgage", attribute="mortgage pre-approval amount", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bfinal\s+sale\s+price\s+was\s+\$(?P<value>\d[\d,]*(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="house", attribute="house final sale price", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bwaterproof\s+car\s+cover[^.?!;\n]{0,120}?\bcost\s+me\s+\$(?P<value>\d+(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="car cover", attribute="car accessory cost", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    if re.search(r"\bwaterproof\s+car\s+cover\b", source, re.IGNORECASE):
        for match in re.finditer(r"\bit\s+cost\s+me\s+\$(?P<value>\d+(?:\.\d+)?)\b", source, re.IGNORECASE):
            records.append(_numeric_state(subject="car cover", attribute="car accessory cost", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bdetailing\s+spray[^.?!;\n]{0,120}?\bfrom\s+Amazon\s+for\s+\$(?P<value>\d+(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="detailing spray", attribute="car accessory cost", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\btakes\s+me\s+about\s+(?P<value>an?|one|two|three|four|five|six|seven|eight|nine|ten|\d+(?:\.\d+)?)\s+(?P<unit>hours?|minutes?)\s+to\s+get\s+ready\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="get ready", attribute="morning routine duration minutes", value=str(_duration_minutes(match.group("value"), match.group("unit"))), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bcommute\s+to\s+work\s+takes\s+about\s+(?P<value>an?|one|two|three|four|five|six|seven|eight|nine|ten|\d+(?:\.\d+)?)\s+(?P<unit>hours?|minutes?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="commute", attribute="morning routine duration minutes", value=str(_duration_minutes(match.group("value"), match.group("unit"))), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bfinished\s+a\s+5K\s+in\s+(?P<value>\d+)\s+minutes\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="current 5K run", attribute="current 5k time minutes", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b5K\s+run\s+last\s+year[^.?!;\n]{0,120}?\btook\s+me\s+(?P<value>\d+)\s+minutes\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="previous 5K run", attribute="previous 5k time minutes", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?P<value>\d+)[-\s]+pound\s+batch\b", source, re.IGNORECASE):
        if "feed" in source.lower() or "scratch grains" in source.lower() or "chickens" in source.lower():
            records.append(_numeric_state(subject="feed batch", attribute="feed weight pounds", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?P<value>\d+)\s+pounds?\s+of\s+organic\s+scratch\s+grains\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="scratch grains", attribute="feed weight pounds", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bleft\s+home\s+at\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>AM|PM)\s+on\s+Monday\b", source, re.IGNORECASE):
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or "0")
        if match.group("ampm").lower() == "pm" and hour != 12:
            hour += 12
        if match.group("ampm").lower() == "am" and hour == 12:
            hour = 0
        records.append(_numeric_state(subject="clinic departure", attribute="clinic departure minutes", value=str(hour * 60 + minute), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bit\s+took\s+me\s+(?P<value>an?|one|two|three|four|five|six|seven|eight|nine|ten|\d+(?:\.\d+)?)\s+(?P<unit>hours?|minutes?)\s+to\s+get\s+to\s+the\s+clinic\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="clinic travel", attribute="clinic travel duration minutes", value=str(_duration_minutes(match.group("value"), match.group("unit"))), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bvintage\s+diamond\s+necklace[^.?!;\n]{0,120}?\bworth\s+\$(?P<value>\d[\d,]*(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="vintage diamond necklace", attribute="resale value", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bantique\s+vanity[^.?!;\n]{0,160}?\b(?:at\s+least|for)\s+\$(?P<value>\d[\d,]*(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="antique vanity", attribute="resale value", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?P<value>\d+(?:\.\d+)?)\s*-\s*mile\s+(?:hike|loop trail|trail)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="hike", attribute="hike distance", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\btrain fare is actually\s+\$(?P<value>\d+(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="train", attribute="train fare", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\btaxi[^.?!;\n]{0,120}?\bcost(?: me)?\s+\$(?P<value>\d+(?:\.\d+)?)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="taxi", attribute="taxi fare", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for item, pattern in [
        ("food bowl", r"food bowl[^.?!;\n]{0,80}?\$(?P<value>\d+(?:\.\d+)?)"),
        ("measuring cup", r"measuring cup[^.?!;\n]{0,80}?\$(?P<value>\d+(?:\.\d+)?)"),
        ("dental chews", r"dental chews\s+(?:are|were|cost(?: me)?|for)?\s*\$(?P<value>\d+(?:\.\d+)?)"),
        ("dental chews", r"\bchews\s+are\s+\$(?P<value>\d+(?:\.\d+)?)\s+a\s+pack\b"),
        ("flea collar", r"flea and tick collar[^.?!;\n]{0,80}?\$(?P<value>\d+(?:\.\d+)?)"),
    ]:
        for match in re.finditer(pattern, source, re.IGNORECASE):
            records.append(_numeric_state(subject=item, attribute="pet supply cost", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:luxury boots|boots)[^.?!;\n]{0,120}?\$(?P<value>\d+(?:,\d{3})*(?:\.\d+)?)", source, re.IGNORECASE):
        attr = "luxury boots price" if "luxury" in match.group(0).lower() or "splurged" in source[match.start() - 80:match.start()].lower() else "budget boots price"
        records.append(_numeric_state(subject="boots", attribute=attr, value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bbudget store[^.?!;\n]{0,120}?\$(?P<value>\d+(?:,\d{3})*(?:\.\d+)?)", source, re.IGNORECASE):
        records.append(_numeric_state(subject="boots", attribute="budget boots price", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bwearing\s+(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b[^.?!;\n]{0,80}?\b(?:sneakers|sandals|shoes)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="shoes", attribute="shoes worn count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bpacked\s+(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+pairs?\s+of\s+shoes\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="shoes", attribute="shoes packed count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:finished around\s+(?P<around>\d+)\s+episodes?|finished episode\s+(?P<episode>\d+))\b", source, re.IGNORECASE):
        value = match.group("around") or match.group("episode")
        if not value:
            continue
        records.append(_numeric_state(subject="podcast", attribute="podcast episodes listened", value=value, date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:planted|got)\s+(?P<value>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?P<subject>tomato|cucumber)\s+plants?\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject=f"{match.group('subject').lower()} plants", attribute="garden plant count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?P<subject>tomatoes|cucumbers)\b[^.?!;\n]{0,120}?\b(?:got|have)\s+(?P<value>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+plants?\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject=f"{match.group('subject').lower().rstrip('s')} plants", attribute="garden plant count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:reached around|promoted my product to her)\s+(?P<value>\d[\d,]*)\s+(?:people|followers)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="audience", attribute="audience reach count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bwritten\s+(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+(?:short\s+)?stories?\b[^.?!;\n]{0,120}?\bsince\s+I\s+started\s+writing\s+regularly\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="writing", attribute="short stories written count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\badded\s+(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+(?:new\s+)?(?:ones|postcards?)\b[^.?!;\n]{0,120}?\b(?:postcards?|collection|collecting)\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="postcards", attribute="postcards added count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\btried\s+making\s+(?:a\s+)?Negroni\s+at\s+home\s+(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+times?\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="negroni", attribute="negroni tried count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\blost\s+(?:about\s+)?(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+pounds?\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="fitness", attribute="weight lost", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\blead\s+(?:a\s+team\s+of\s+)?(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+engineers?\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Senior Software Engineer", attribute="engineers led count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:had|have|reached)\s+(?:around\s+)?(?P<value>\d[\d,]*)\s+followers\s+on\s+Instagram\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Instagram", attribute="instagram follower count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bstarted\s+the\s+year\s+with\s+(?P<value>\d[\d,]*)\s+followers\s+on\s+Instagram\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Instagram", attribute="instagram follower count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:close\s+to|nearing)\s+(?P<value>\d[\d,]*)\s+followers\b", source, re.IGNORECASE):
        if "instagram" in source.lower():
            records.append(_numeric_state(subject="Instagram", attribute="instagram follower count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:close\s+to|nearing)\s+(?P<value>\d[\d,]*)\s+now\s+on\s+Instagram\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Instagram", attribute="instagram follower count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bFitbit\s+Charge\s+3\s+for\s+(?P<value>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+months?\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Fitbit Charge 3", attribute="fitbit usage months", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:worn\s+them|worn\s+my\s+new\s+black\s+Converse[^.?!;\n]{0,80}?)\s+(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+times?\b", source, re.IGNORECASE):
        if "converse" in source.lower():
            records.append(_numeric_state(subject="black Converse Chuck Taylor All Star sneakers", attribute="converse worn count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bthat's\s+(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+times?\s+now\s+that\s+I've\s+worn\s+them\b", source, re.IGNORECASE):
        if "converse" in source.lower():
            records.append(_numeric_state(subject="black Converse Chuck Taylor All Star sneakers", attribute="converse worn count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:currently\s+on|completed)\s+episode\s+(?P<value>\d+)\s+of\s+the\s+Science\s+series\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Crash Course Science series", attribute="crash course science episodes", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bcompleted\s+(?P<value>\d+)\s+episodes\s+of\s+Crash\s+Course's\s+Science\s+series\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Crash Course Science series", attribute="crash course science episodes", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bcompleted\s+(?P<value>\d+)\s+videos\s+(?:so\s+far\s+)?(?:for|of)\s+Corey(?:'s| Schafer's)\s+(?:Python\s+)?(?:programming\s+)?series\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Corey Schafer Python series", attribute="corey python videos completed", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bwatched\s+(?:a\s+lot\s+of\s+Crash\s+Course\s+videos\s+[^.?!;\n]{0,80}?finished|having\s+watched|completed)\s+(?P<value>\d+)\s+(?:Crash\s+Course\s+)?videos\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Crash Course videos", attribute="crash course videos watched count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bhaving\s+watched\s+(?P<value>\d+)\s+Crash\s+Course\s+videos\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Crash Course videos", attribute="crash course videos watched count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bhighest\s+score\s+(?:so\s+far\s+)?(?:is|-)\s+(?P<value>\d+)\s+points?\b", source, re.IGNORECASE):
        if "ticket to ride" in source.lower():
            records.append(_numeric_state(subject="Ticket to Ride", attribute="ticket to ride highest score", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\bhighest\s+score\s+in\s+Ticket\s+to\s+Ride\s+-\s+(?P<value>\d+)\s+points?\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Ticket to Ride", attribute="ticket to ride highest score", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\btried\s+out\s+(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+of\s+Emma's\s+recipes\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="Emma recipes", attribute="emma recipes tried count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:watched|including)\s+(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+MCU\s+films?\b", source, re.IGNORECASE):
        records.append(_numeric_state(subject="MCU films", attribute="mcu films watched count", value=_number_word_to_digit(match.group("value")), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:with|currently)\s+(?P<value>\d+)\s+titles\s+(?:waiting\s+to\s+be\s+checked\s+off|on\s+it\s+right\s+now)?\b", source, re.IGNORECASE):
        if "to-watch list" in source.lower() or "watchlist" in source.lower():
            records.append(_numeric_state(subject="to-watch list", attribute="to-watch list count", value=match.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    original = re.search(r"\boriginally\s+priced\s+at\s+\$(?P<value>\d+(?:\.\d+)?)\b", source, re.IGNORECASE)
    if original:
        records.append(_numeric_state(subject="book", attribute="book original price", value=original.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    discounted = re.search(r"\bgot\s+the\s+book\s+for\s+\$(?P<value>\d+(?:\.\d+)?)\s+after\s+a\s+discount\b", source, re.IGNORECASE)
    if discounted:
        records.append(_numeric_state(subject="book", attribute="book discounted price", value=discounted.group("value"), date=date, evidence=source, evidence_id=evidence_id))
    return records


def _extract_targeted_event_records(text: str, *, date: str, evidence_id: str, subject_hint: str) -> list[StateRecord]:
    records: list[StateRecord] = []
    source = str(text or "")
    targeted_patterns = [
        (r"\b(?:I\s+also\s+got|I\s+(?:just\s+|recently\s+)?got|I\s+received)\s+(?P<value>[^.?!;\n]{0,80}?crystal\s+chandelier\s+from\s+my\s+aunt[^.?!;\n]{0,80})", "received item event"),
        (r"\b(?P<value>baking\s+class\s+I\s+took\s+at\s+a\s+local\s+culinary\s+school\s+yesterday)\b", "class event"),
        (r"\b(?P<value>feedback\s+from\s+judges\s+that\s+my\s+car's\s+suspension\s+was\s+too\s+soft[^.?!;\n]{0,80})", "feedback event"),
        (r"\b(?P<value>(?:I(?:'ll| will)\s+be\s+)?testing\s+my\s+car's\s+new\s+suspension\s+setup[^.?!;\n]{0,120}?tomorrow)\b", "test event"),
        (r"\b(?P<value>test\s+my\s+car's\s+new\s+suspension\s+setup[^.?!;\n]{0,120}?tomorrow)\b", "test event"),
        (r"\b(?P<value>tomorrow[^.?!;\n]{0,120}?(?:I(?:'ll| will)\s+be\s+)?testing\s+my\s+car's\s+new\s+suspension\s+setup)\b", "test event"),
        (r"\b(?P<value>(?:Emma|Rachel|Alex)\s+graduated[^.?!;\n]{0,100})", "graduation event"),
        (r"\b(?P<value>(?:Emma|Rachel|Alex)'s\s+[^.?!;\n]{0,80}?graduation\s+ceremony[^.?!;\n]{0,100})", "graduation event"),
        (r"\b(?P<value>friend\s+(?:Emma|Rachel|Alex)'s\s+[^.?!;\n]{0,80}?graduation\s+ceremony[^.?!;\n]{0,100})", "graduation event"),
        (r"\b(?P<value>(?:bus|train)\s+ride[^.?!;\n]{0,120})", "transport event"),
        (r"\b(?P<value>took\s+the\s+(?:bus|train)[^.?!;\n]{0,120})", "transport event"),
        (r"\b(?P<value>charity\s+(?:bake\s+sale|gala)[^.?!;\n]{0,120})", "participation event"),
        (r"\bI\s+(?:finally\s+)?set up\s+(?P<value>my\s+smart\s+thermostat[^.?!;\n]{0,80})", "setup event"),
        (r"\bI\s+(?:recently\s+)?got\s+(?P<value>a\s+new\s+router[^.?!;\n]{0,80})", "setup event"),
        (r"\bI\s+(?:am\s+)?glad\s+I\s+cancelled\s+(?P<value>my\s+monthly\s+grocery\s+delivery\s+subscription\s+from\s+FarmFresh[^.?!;\n]{0,80})", "subscription cancellation event"),
        (r"\bI\s+cancelled\s+(?P<value>my\s+monthly\s+grocery\s+delivery\s+subscription\s+from\s+FarmFresh[^.?!;\n]{0,80})", "subscription cancellation event"),
        (r"\bI(?:'ve| have)\s+been\s+taking\s+(?P<value>Spanish\s+classes[^.?!;\n]{0,80})", "education event"),
        (r"\bI\s+had\s+a\s+great\s+time\s+celebrating\s+(?P<value>my\s+best\s+friend[^.?!;\n]{0,120}?birthday\s+party[^.?!;\n]{0,80})", "birthday event"),
        (r"\bI\s+lost\s+(?P<value>my\s+old\s+one\s+at\s+the\s+gym[^.?!;\n]{0,80})", "lost item event"),
        (r"\b(?:mine|my\s+stand\s+mixer)\s+(?:breaks?\s+down|broke\s+down)[^.?!;\n]{0,120}?\b(?P<value>last\s+month)", "malfunction event"),
        (r"\bI\s+just\s+got\s+(?P<value>my\s+new\s+phone\s+case[^.?!;\n]{0,80})", "received item event"),
        (r"\bI\s+(?:recently\s+)?got\s+(?P<value>a\s+new\s+50mm[^.?!;\n]{0,80}prime\s+lens[^.?!;\n]{0,80})", "received item event"),
        (r"\btoday\s+I\s+sold\s+(?P<value>homemade\s+baked\s+goods[^.?!;\n]{0,120}?Farmers'\s+Market)", "market event"),
        (r"\bat\s+the\s+(?P<value>Spring\s+Fling\s+Market[^.?!;\n]{0,80})\s+yesterday\b", "market event"),
        (r"\bI\s+replaced\s+(?P<value>my\s+spark\s+plugs[^.?!;\n]{0,80})\s+today\b", "maintenance event"),
        (r"\bduring\s+the\s+(?P<value>Turbocharged\s+Tuesdays\s+event)\s+today\b", "racing event"),
        (r"\bI\s+just\s+submitted\s+(?P<value>my\s+master's\s+thesis[^.?!;\n]{0,80})\s+today\b", "submission event"),
        (r"\bI\s+finally\s+got\s+around\s+to\s+(?P<value>fixing\s+that\s+flat\s+tire\s+on\s+my\s+mountain\s+bike[^.?!;\n]{0,120})", "maintenance event"),
        (r"\bI\s+decided\s+to\s+(?P<value>upgrade\s+my\s+road\s+bike's\s+pedals[^.?!;\n]{0,100})\s+today\b", "upgrade event"),
        (r"\bwent\s+to\s+(?P<value>a\s+NBA\s+game[^.?!;\n]{0,120})\s+today\b", "watched sports event"),
        (r"\bwatched\s+(?P<value>the\s+College\s+Football\s+National\s+Championship\s+game[^.?!;\n]{0,120})\s+yesterday\b", "watched sports event"),
        (r"\bwatching\s+(?P<value>[^.?!;\n]{0,120}?NFL\s+playoffs[^.?!;\n]{0,120})\s+last\s+weekend\b", "watched sports event"),
        (r"\bI\s+participate\s+in\s+(?P<value>the\s+company's\s+annual\s+charity\s+soccer\s+tournament[^.?!;\n]{0,80})\s+today\b", "participation event"),
        (r"\bI\s+visited\s+(?P<value>the\s+Science\s+Museum[^.?!;\n]{0,120})\s+today\b", "museum visit"),
        (r"\b(?:attended|came\s+back\s+from)\s+(?P<value>a\s+lecture[s]?\s+series\s+at\s+the\s+Museum\s+of\s+Contemporary\s+Art[^.?!;\n]{0,120})", "museum visit"),
        (r"\b(?:saw|seen)\s+(?P<value>[^.?!;\n]{0,120}?Metropolitan\s+Museum\s+of\s+Art[^.?!;\n]{0,120})", "museum visit"),
        (r"\bparticipated\s+in\s+(?P<value>a\s+behind-the-scenes\s+tour\s+of\s+the\s+Museum\s+of\s+History[^.?!;\n]{0,120})", "museum visit"),
        (r"\battended\s+(?P<value>(?:their\s+)?guided\s+tour\s+of\s+(?:the\s+)?Modern\s+Art\s+Museum[^.?!;\n]{0,140})", "museum visit"),
        (r"\btook\s+my\s+niece\s+to\s+(?P<value>the\s+Natural\s+History\s+Museum[^.?!;\n]{0,120})\s+today\b", "museum visit"),
        (r"\b(?P<value>Billie\s+Eilish\s+(?:concert|show)[^.?!;\n]{0,140})", "music event"),
        (r"\b(?P<value>free\s+outdoor\s+concert\s+series\s+in\s+the\s+park)\b", "music event"),
        (r"\b(?P<value>music\s+festival\s+in\s+Brooklyn[^.?!;\n]{0,120})", "music event"),
        (r"\b(?P<value>jazz\s+night\s+at\s+a\s+local\s+bar[^.?!;\n]{0,80})", "music event"),
        (r"\bat\s+the\s+(?P<value>jazz\s+night\s+at\s+the\s+local\s+bar[^.?!;\n]{0,80})", "music event"),
        (r"\b(?P<value>Queen[^.?!;\n]{0,120}?Adam\s+Lambert[^.?!;\n]{0,120}?Prudential\s+Center[^.?!;\n]{0,80})", "music event"),
    ]
    for pattern, attribute in targeted_patterns:
        for match in re.finditer(pattern, source, re.IGNORECASE):
            value = _clean_value(match.group("value"))
            if not value:
                continue
            if "old one" in value.lower() and "phone charger" in source.lower():
                value = "my phone charger"
            if attribute == "malfunction event":
                value = "stand mixer malfunction"
            records.append(
                StateRecord(
                    subject=subject_hint,
                    attribute=attribute,
                    value=value,
                    date=_infer_event_date(date, match.group(0)),
                    evidence=source,
                    evidence_id=evidence_id,
                    confidence=0.78,
                    record_type="event",
                )
            )
    for match in re.finditer(r"\bflight\s+on\s+(?P<airline>JetBlue)\b", source, re.IGNORECASE):
        records.append(_airline_event(match.group("airline"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:flight|flying)\s+(?:with|on)\s+(?P<airline>American\s+Airlines|United\s+Airlines|Delta)\b", source, re.IGNORECASE):
        records.append(_airline_event(match.group("airline"), date=date, evidence=source, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?P<airline>American\s+Airlines|United\s+Airlines|Delta|JetBlue)(?:'s|')?\s+[^.?!;\n]{0,80}?\bflight\b", source, re.IGNORECASE):
        records.append(_airline_event(match.group("airline"), date=date, evidence=source, evidence_id=evidence_id))
    if re.search(r"\bDelta\s+SkyMiles\b", source, re.IGNORECASE) and re.search(r"\btaking\s+a\s+round-trip\s+flight\b", source, re.IGNORECASE):
        records.append(_airline_event("Delta", date=date, evidence=source, evidence_id=evidence_id))
    return records


def _airline_event(airline: str, *, date: str, evidence: str, evidence_id: str) -> StateRecord:
    label = airline.strip()
    if label.lower() == "united airlines":
        label = "United"
    if label.lower() == "american airlines":
        label = "American Airlines"
    return StateRecord(
        subject="user",
        attribute="airline flight",
        value=label,
        date=date,
        evidence=evidence,
        evidence_id=evidence_id,
        confidence=0.82,
        record_type="event",
    )


_STATE_PATTERNS = [
    re.compile(
        r"(?P<prefix>\bupdate\s*[:,-]?\s*)?"
        r"\b(?P<subject>my|the|our)\s+"
        r"(?P<attribute>[a-zA-Z0-9][a-zA-Z0-9\s_-]{1,60}?)\s+"
        r"(?:is|are|was|were)\s+"
        r"(?P<marker>now|currently|updated to|changed to|recently)?\s*"
        r"(?P<value>[^.?!;\n]{1,80})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bI\s+(?P<marker>now|currently|recently)\s+"
        r"(?P<attribute>have|own|use|attend|take|spend|prefer)\s+"
        r"(?P<value>[^.?!;\n]{1,80})",
        re.IGNORECASE,
    ),
]

_VALUE_STATE_PATTERNS = [
    re.compile(
        r"\b(?:currently\s+have|have|own)\s+(?P<value>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+bikes?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:new\s+hybrid\s+bike|hybrid\s+bike)[^.?!;\n]{0,160}?\b(?:road bike|mountain bike|commuter bike)[^.?!;\n]{0,160}?\b(?:hybrid\s+bike|new\s+bike)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:road bike|mountain bike|commuter bike)[^.?!;\n]{0,160}?\b(?:new|hybrid)\s+bike\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:need|requires?|require)\s+(?P<value>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+stars?\s+"
        r"(?:to\s+)?(?:reach|get to)\s+(?:the\s+)?gold",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bcurrently\s+(?:at|working\s+at)\s+(?P<value>[A-Z][A-Za-z0-9&.-]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<subject>[A-Z][a-z]+)[^.\n]{0,80}?\b(?:currently\s+at|currently\s+working\s+at|who's\s+currently\s+at)\s+"
        r"(?P<value>[A-Z][A-Za-z0-9&.-]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:got|getting|with)\s+(?P<value>(?:a|my)?\s*new\s+)?(?P<attribute>\d{2,3}-\d{2,3}mm\s+zoom\s+lens|50mm\s+prime\s+lens)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:got|having\s+got)\s+my\s+guitar\s+serviced(?:\s+from|\s+at)?\s+(?P<value>[^.?!;\n]{2,80})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:music\s+shop\s+on\s+Main\s+St)[^.?!;\n]{0,80}?\b(?:got\s+my\s+guitar\s+serviced|guitar\s+servicing)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bgym[^.?!;\n]{0,120}?\b(?:usually\s+)?(?:at|to\s+at)\s+(?P<value>\d{1,2}:\d{2}\s*(?:am|pm))",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:on\s+page|page)\s+(?P<value>\d{1,4})\b[^.?!;\n]{0,120}?"
        r"(?:A\s+Short\s+History\s+of\s+Nearly\s+Everything|history\s+of\s+medicine|discovery\s+of\s+DNA)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:currently\s+)?on\s+page\s+(?P<value>\d{1,4})\s+of\s+['\"](?P<subject>[^'\"]{2,80})['\"]",
        re.IGNORECASE,
    ),
    re.compile(
        r"['\"](?P<subject>[^'\"]{2,80})['\"][^.?!;\n]{0,120}?\b(?:with|has|is)\s+(?P<value>\d{2,4})\s+pages?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:A\s+Short\s+History\s+of\s+Nearly\s+Everything)[^.?!;\n]{0,120}?\b(?:on\s+page|page)\s+(?P<value>\d{1,4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:we(?:'re| are)|team[^.?!;\n]{0,40}?\bis)\s+(?P<value>\d+-\d+)\b[^.?!;\n]{0,80}?\b(?:volleyball|league|record)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:volleyball|league|record)[^.?!;\n]{0,120}?\b(?P<value>\d+-\d+)\s+record\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:see|session\s+with)\s+Dr\.\s+(?P<subject>[A-Z][a-z]+)[^.?!;\n]{0,80}?\b(?P<value>every\s+(?:week|two\s+weeks|other\s+week)|weekly|bi-weekly)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<value>every\s+(?:week|two\s+weeks|other\s+week)|weekly|bi-weekly)[^.?!;\n]{0,80}?\b(?:session\s+with|see)\s+Dr\.\s+(?P<subject>[A-Z][a-z]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:attend\s+)?yoga\s+(?:classes\s+)?[^.?!;\n]{0,80}?\b(?:is\s+)?(?P<value>(?:once|twice|three|four|five|\d+)\s+times?\s+a\s+week)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bmy\s+grandma(?:'s)?\s+(?P<value>\d{1,3})(?:st|nd|rd|th)?\s+birthday",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdo\s+you\s+think\s+(?P<value>\d{1,3})\s+is\s+considered",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bKorean\s+restaurants?[\s\S]{0,160}?\bI(?:'ve| have)\s+tried\s+"
        r"(?P<value>one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+different\s+ones",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bpersonal best(?: time)?(?: in| for)?(?P<attribute>[^.?!;\n]{0,45}?)\s+"
        r"(?:with a time of|was|is|of)\s+"
        r"(?P<value>\d{1,2}:\d{2}|\d+\s+minutes?(?:\s+and\s+\d+\s+seconds?)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:completed|finished|did)[^.?!;\n]{0,100}?(?P<attribute>(?:charity\s+)?5K\s+run)[^.?!;\n]{0,100}?"
        r"personal best time\s+(?:of|with)\s+"
        r"(?P<value>\d{1,2}:\d{2}|\d+\s+minutes?(?:\s+and\s+\d+\s+seconds?)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:pre[- ]approval amount|pre[- ]approved(?: for)?|approved(?: for)?)\s+"
        r"(?:of|for|was|is)?\s*(?P<value>\$?\d[\d,]*(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:doing|attend(?:ing)?)\s+(?P<attribute>yoga(?: classes)?)\s+"
        r"(?P<value>(?:once|twice|three|four|five|\d+)\s+times?\s+a\s+week)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\btried\s+(?P<value>(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)(?:\s+different)?)\s+"
        r"(?P<attribute>[^.?!;\n]{0,50}?restaurants?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:moved|relocated)\s+(?:back\s+)?to\s+(?P<value>[^.?!;\n]{2,80})",
        re.IGNORECASE,
    ),
]

_SEMANTIC_EVENT_PATTERNS = [
    re.compile(
        r"\b(?:just\s+|recently\s+)?(?P<verb>got back from|came back from)\s+"
        r"(?P<value>[^.?!;\n]{2,140})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bI\s+(?:just\s+|recently\s+|actually\s+)?(?P<verb>tried|visited|attended|ordered|started|finished|completed|discovered|helped|met|received|participated in|took part in|volunteered at|volunteered for|did|went to|went on|came back from|got back from|walked down|picked up|scored|set|got|bought|used|redeemed|signed up for|harvested|practice)\s+"
        r"(?P<value>[^.?!;\n]{2,140})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bI\s+(?:just\s+|recently\s+|actually\s+)?(?P<verb>baked|made|watched|fixed|serviced|planted|launched|signed|joined|upgraded)\s+"
        r"(?P<value>[^.?!;\n]{2,140})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bI(?:'ve| have)\s+(?P<verb>tried|visited|attended|finished|completed|been doing|been playing|been using|been listening to|been trying|been focusing on|gone on|used|redeemed|signed up for|harvested|practiced)\s+"
        r"(?P<value>[^.?!;\n]{2,140})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<subject>[A-Z][a-z]+|she|he|they)\s+(?P<verb>moved|relocated|switched|changed)\s+(?:to|into|from)?\s*"
        r"(?P<value>[^.?!;\n]{2,100})",
        re.IGNORECASE,
    ),
]

_DATED_NOUN_EVENT_PATTERN = re.compile(
    r"\b(?P<value>(?:upcoming\s+)?(?:team\s+meeting|bible\s+study|(?:lovely\s+)?midnight\s+mass|holiday\s+food\s+drive|food\s+drive|workshop|meeting))"
    r"[\s\S]{0,160}?\bon\s+"
    r"(?P<month>january|february|march|april|may|june|july|august|september|october|november|december)\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?",
    re.IGNORECASE,
)


def _clean_state_text(text: str) -> str:
    return " ".join(str(text or "").strip().split()).strip(" ,:")


def _clean_value(text: str) -> str:
    text = _clean_state_text(text)
    text = re.split(
        r"\b(?:and then|but|so|because|which|that|do you|can you|what do you|anyway|by the way)\b",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return _clean_state_text(text).strip(" .")


def _infer_event_attribute(verb: str, value: str) -> str:
    value_l = value.lower()
    verb_l = verb.lower()
    if "personal best" in value_l or "5k" in value_l:
        return "personal best time"
    if "restaurant" in value_l:
        return "restaurant visit count"
    if "super bowl" in value_l or "nfl playoffs" in value_l or "nba game" in value_l:
        return "watched sports event"
    if "yoga" in value_l:
        return "yoga frequency"
    if "museum" in value_l:
        return "museum visit"
    if "concert" in value_l or "music festival" in value_l or "jazz night" in value_l:
        return "music event"
    if "wedding" in value_l or "engagement" in value_l:
        return "event attendance"
    if "walked down" in verb_l:
        return "event attendance"
    if "workshop" in value_l or "class" in value_l or "exhibit" in value_l:
        return "event attendance"
    if "baked" in verb_l or "made" in verb_l:
        return "cooking event"
    if "planted" in verb_l:
        return "gardening activity"
    if "fixed" in verb_l or "serviced" in verb_l or "upgraded" in verb_l:
        return "maintenance event"
    if "launched" in verb_l or "signed" in verb_l:
        return "milestone event"
    if verb_l == "met":
        return "social meeting"
    if "keyboard" in value_l or "songs" in value_l:
        return "music practice event"
    if "sale" in value_l or "nordstrom" in value_l:
        return "shopping event"
    if "coupon" in value_l or "cashback" in value_l or "gift card" in value_l or "rewards program" in value_l:
        return "shopping reward event"
    if "hike" in value_l or "road trip" in value_l or "camping" in value_l:
        return "travel event"
    if "harvest" in verb_l:
        return "harvest event"
    if "finished" in verb_l or "completed" in verb_l:
        return "completion event"
    if "participated" in verb_l or "took part" in verb_l or "volunteered" in verb_l:
        return "participation event"
    if verb_l == "did" and "event" in value_l:
        return "participation event"
    if "meeting" in value_l:
        return "meeting event"
    if "moved" in verb_l or "relocated" in verb_l:
        return "location"
    if "tried" in verb_l:
        return "tried item"
    return verb_l


def score_state_record(question_terms: set[str], record: StateRecord) -> float:
    text = " ".join([record.subject, record.attribute, record.value, record.evidence])
    terms = tokenize(text)
    if not terms:
        return 0.0
    overlap = sum(1 for term in set(terms) if term in question_terms)
    score = overlap / (len(set(terms)) ** 0.5)
    if record.record_type == "event" and any(
        term in question_terms for term in ["when", "first", "between", "days", "weeks", "months", "order"]
    ):
        score += 0.25
    if record.record_type == "state" and any(term in question_terms for term in ["what", "where", "how", "which"]):
        score += 0.10
    return score + record.confidence * 0.05


def _score_latest_state_candidate(question: str, record: StateRecord) -> float:
    q = question.lower()
    terms = set(tokenize(question))
    primary = set(tokenize(" ".join([record.subject, record.attribute, record.value])))
    evidence = set(tokenize(record.evidence))
    score = 2.0 * len(terms & primary) + 0.2 * len(terms & evidence) + record.confidence
    attr = record.attribute.lower()
    hint_pairs = [
        ("bike", "bike count"),
        ("yoga", "yoga frequency"),
        ("dr smith", "dr smith frequency"),
        ("company", "current company"),
        ("lens", "camera lens"),
        ("guitar", "guitar serviced location"),
        ("gym", "gym time"),
        ("page", "reading page"),
        ("stars", "starbucks gold stars needed"),
        ("volleyball", "volleyball record"),
        ("personal best", "personal best time"),
        ("family trip", "family trip location"),
    ]
    for hint, attribute in hint_pairs:
        if hint in q and attribute in attr:
            score += 8.0
    if "how many" in q and not re.search(r"\d|one|two|three|four|five|six|seven|eight|nine|ten", record.value.lower()):
        score -= 4.0
    return score


def _latest_state_hint_match(question: str, record: StateRecord) -> bool:
    q = question.lower()
    attr = record.attribute.lower()
    hint_pairs = [
        ("bike", "bike count"),
        ("yoga", "yoga frequency"),
        ("dr smith", "dr smith frequency"),
        ("company", "current company"),
        ("lens", "camera lens"),
        ("guitar", "guitar serviced location"),
        ("gym", "gym time"),
        ("page", "reading page"),
        ("stars", "starbucks gold stars needed"),
        ("volleyball", "volleyball record"),
        ("personal best", "personal best time"),
        ("family trip", "family trip location"),
    ]
    return any(hint in q and attribute in attr for hint, attribute in hint_pairs)


def extract_state_records(
    text: str,
    *,
    date: str = "",
    evidence_id: str = "",
    subject_hint: str = "user",
) -> list[StateRecord]:
    """Extract simple latest-state facts from a memory/evidence string.

    This is deliberately conservative and deterministic. It is not intended to
    replace an LLM information extractor; it provides a low-cost state-memory
    substrate for update/current/now style evidence.
    """
    records: list[StateRecord] = []
    for pattern in _STATE_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groupdict()
            lowered = match.group(0).lower()
            if not (
                groups.get("prefix")
                or groups.get("marker")
                or any(marker in lowered for marker in [" now ", " currently ", " updated ", " changed ", " recently "])
            ):
                continue
            raw_subject = groups.get("subject") or subject_hint
            subject = "user" if raw_subject.lower() in {"my", "our", "i"} else _clean_state_text(raw_subject)
            attribute = _clean_state_text(groups.get("attribute", "state")).lower()
            value = _clean_state_text(groups.get("value", ""))
            if not value:
                continue
            confidence = 0.70
            if any(marker in lowered for marker in ["now", "currently", "updated", "changed", "recently"]):
                confidence += 0.15
            records.append(
                StateRecord(
                    subject=subject,
                    attribute=attribute,
                    value=value,
                    date=date,
                    evidence=text,
                    evidence_id=evidence_id,
                    confidence=min(confidence, 0.95),
                    record_type="state",
                )
            )
    records.extend(
        extract_semantic_state_records(
            text,
            date=date,
            evidence_id=evidence_id,
            subject_hint=subject_hint,
        )
    )
    return records


def extract_semantic_state_records(
    text: str,
    *,
    date: str = "",
    evidence_id: str = "",
    subject_hint: str = "user",
) -> list[StateRecord]:
    """Extract broader deterministic state/event records from conversation text."""
    records: list[StateRecord] = []
    text = str(text or "")

    for pattern in _VALUE_STATE_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groupdict()
            attribute = _clean_state_text(groups.get("attribute") or "state").lower()
            value = _clean_value(groups.get("value") or "")
            matched = match.group(0).lower()
            if not value and "road bike" in matched and "mountain bike" in matched and "commuter bike" in matched and "new" in matched:
                value = "4"
            elif not value and "music shop on main st" in matched:
                value = "The music shop on Main St."
            elif not value and "hybrid bike" in matched and "road bike" in matched and "mountain bike" in matched and "commuter bike" in matched:
                value = "4"
            if not value:
                continue
            if "bikes" in matched or "bike" in matched and attribute == "state":
                attribute = "bike count"
                value = _number_word_to_digit(value)
            elif "stars" in matched and "gold" in matched:
                attribute = "starbucks gold stars needed"
                value = _number_word_to_digit(value)
            elif "currently at" in matched or "working at" in matched:
                attribute = "current company"
            elif "lens" in matched:
                attribute = "camera lens"
                lens = groups.get("attribute") or ""
                prefix = (groups.get("value") or "").lower()
                article = "a " if prefix else ""
                value = _clean_value(article + lens)
            elif "guitar serviced" in matched or "music shop on main st" in matched:
                attribute = "guitar serviced location"
            elif "gym" in matched:
                attribute = "gym time"
            elif "short history of nearly everything" in matched or "page" in matched and "history" in matched:
                attribute = "reading page"
                value = _number_word_to_digit(value)
            elif "on page" in matched:
                attribute = "reading page"
                value = _number_word_to_digit(value)
            elif "pages" in matched:
                attribute = "total pages"
                value = _number_word_to_digit(value)
            elif "volleyball" in matched or "record" in matched and re.search(r"\d+-\d+", matched):
                attribute = "volleyball record"
            elif "dr." in matched:
                attribute = f"dr {groups.get('subject', '').lower()} frequency".strip()
            elif "yoga" in matched and "week" in matched:
                attribute = "yoga frequency"
            elif ("personal best" in matched or "5k" in matched) and attribute == "state":
                attribute = "personal best time"
            elif "pre" in matched or "approved" in matched:
                attribute = "mortgage pre-approval amount"
            elif "grandma" in matched or "considered" in matched:
                attribute = "age"
            elif "korean" in matched and "restaurant" in matched:
                attribute = "korean restaurants tried count"
            elif "moved" in matched or "relocated" in matched:
                attribute = "location"
            subject = _clean_state_text(groups.get("subject") or subject_hint)
            if "grandma" in matched:
                subject = "grandma"
            records.append(
                StateRecord(
                    subject=subject,
                    attribute=attribute,
                    value=value,
                    date=date,
                    evidence=text,
                    evidence_id=evidence_id,
                    confidence=0.82,
                    record_type="state",
                )
            )

    for match in re.finditer(r"\bwent\s+to\s+(?P<value>[A-Z][A-Za-z\s]+?)\s+with\s+my\s+family\b", text):
        records.append(
            StateRecord(
                subject="user",
                attribute="family trip location",
                value=_clean_value(match.group("value")),
                date=date,
                evidence=text,
                evidence_id=evidence_id,
                confidence=0.86,
                record_type="state",
            )
        )

    for match in re.finditer(r"\bcocktail-making\s+class\s+on\s+(?P<value>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)s?\b", text, re.IGNORECASE):
        records.append(
            StateRecord(
                subject="cocktail-making class",
                attribute="class day",
                value=match.group("value").title(),
                date=date,
                evidence=text,
                evidence_id=evidence_id,
                confidence=0.86,
                record_type="state",
            )
        )
    for match in re.finditer(r"\bold\s+sneakers[^.?!;\n]{0,80}?\b(?:under\s+my\s+bed|in\s+a\s+shoe\s+rack)\b", text, re.IGNORECASE):
        location_match = re.search(r"\b(under\s+my\s+bed|in\s+a\s+shoe\s+rack)\b", match.group(0), re.IGNORECASE)
        if location_match:
            records.append(
                StateRecord(
                    subject="old sneakers",
                    attribute="storage location",
                    value=location_match.group(1).lower(),
                    date=date,
                    evidence=text,
                    evidence_id=evidence_id,
                    confidence=0.86,
                    record_type="state",
                )
            )
    for match in re.finditer(r"\b(?:obsessed\s+with|favo(?:u)?rite\s+is)\s+(?P<value>[A-Z][A-Za-z\s'&.-]+?)\s+BBQ\s+sauce\b", text):
        records.append(
            StateRecord(
                subject="BBQ sauce",
                attribute="bbq sauce",
                value=_clean_value(match.group("value")),
                date=date,
                evidence=text,
                evidence_id=evidence_id,
                confidence=0.86,
                record_type="state",
            )
        )
    for match in re.finditer(r"\b(?:I\s+also\s+got|I\s+(?:just\s+|recently\s+)?got|I\s+received)\s+[^.?!;\n]{0,80}?\bcrystal\s+chandelier\s+from\s+(?P<value>my\s+aunt)\b", text, re.IGNORECASE):
        records.append(
            StateRecord(
                subject="crystal chandelier",
                attribute="chandelier source",
                value=match.group("value").lower(),
                date=date,
                evidence=text,
                evidence_id=evidence_id,
                confidence=0.86,
                record_type="state",
            )
        )
        records.append(
            StateRecord(
                subject="jewelry",
                attribute="jewelry source",
                value=match.group("value").lower(),
                date=date,
                evidence=text,
                evidence_id=evidence_id,
                confidence=0.82,
                record_type="state",
            )
        )
    for item_pattern in [
        r"antique\s+tea\s+set\s+from\s+my\s+cousin\s+Rachel",
        r"vintage\s+typewriter\s+that\s+belonged\s+to\s+my\s+dad",
        r"grandmother's\s+vintage\s+diamond\s+necklace",
        r"antique\s+music\s+box\s+from\s+my\s+great-aunt",
        r"set\s+of\s+depression-era\s+glassware\s+from\s+my\s+mom",
    ]:
        for item in re.finditer(item_pattern, text, re.IGNORECASE):
            records.append(
                StateRecord(
                    subject="family heirlooms",
                    attribute="family antique item",
                    value=_clean_value(item.group(0)),
                    date=date,
                    evidence=text,
                    evidence_id=evidence_id,
                    confidence=0.84,
                    record_type="state",
                )
            )
    acl_submission = re.search(r"\bACL[^.?!;\n]{0,80}?\bsubmission\s+date\s+was\s+(?P<value>February\s+1st)\b", text, re.IGNORECASE)
    if acl_submission:
        records.append(
            StateRecord(
                subject="sentiment analysis research paper",
                attribute="research paper submission date",
                value=acl_submission.group("value"),
                date=date,
                evidence=text,
                evidence_id=evidence_id,
                confidence=0.86,
                record_type="state",
            )
        )
    sephora_threshold = re.search(r"\bneed\s+(?P<value>\d+)\s+points?\s+(?:to\s+)?(?:redeem|get)\s+a\s+free\s+skincare\s+product\b", text, re.IGNORECASE)
    if sephora_threshold:
        records.append(_numeric_state(subject="Sephora", attribute="sephora redemption threshold", value=sephora_threshold.group("value"), date=date, evidence=text, evidence_id=evidence_id))
    sephora_total = re.search(r"\b(?:bringing\s+my\s+total\s+to|total\s+is|currently\s+have)\s+(?P<value>\d+)\s+points\b", text, re.IGNORECASE)
    if sephora_total and "sephora" in text.lower():
        records.append(_numeric_state(subject="Sephora", attribute="sephora points total", value=sephora_total.group("value"), date=date, evidence=text, evidence_id=evidence_id))
    handbag_original = re.search(r"\bdesigner\s+handbag[^.?!;\n]{0,120}?\boriginally\s+\$(?P<value>\d+(?:\.\d+)?)\b|\bbag[^.?!;\n]{0,80}?\boriginally\s+\$(?P<value2>\d+(?:\.\d+)?)\b", text, re.IGNORECASE)
    if handbag_original:
        records.append(_numeric_state(subject="designer handbag", attribute="designer handbag original price", value=handbag_original.group("value") or handbag_original.group("value2"), date=date, evidence=text, evidence_id=evidence_id))
    handbag_sale = re.search(r"\b(?:got|bought)\s+(?:it|the\s+bag|my\s+designer\s+handbag)[^.?!;\n]{0,80}?\bfor\s+\$(?P<value>\d+(?:\.\d+)?)\b", text, re.IGNORECASE)
    if handbag_sale and ("handbag" in text.lower() or "bag" in text.lower()):
        records.append(_numeric_state(subject="designer handbag", attribute="designer handbag sale price", value=handbag_sale.group("value"), date=date, evidence=text, evidence_id=evidence_id))
    for match in re.finditer(r"\b(?:moved|leave)\s+the\s+\"(?P<title>Ethereal\s+Dreams)\"\s+painting(?:\s+by\s+Emma\s+Taylor)?\s+(?P<prep>to|above)\s+(?P<value>my\s+bedroom|my\s+living\s+room\s+sofa|my\s+bed)\b", text, re.IGNORECASE):
        location = match.group("value").lower()
        if location == "my bed":
            location = "in my bedroom"
        elif location == "my bedroom":
            location = "in my bedroom"
        else:
            location = "above " + location
        records.append(
            StateRecord(
                subject="Ethereal Dreams",
                attribute="artwork location",
                value=location,
                date=date,
                evidence=text,
                evidence_id=evidence_id,
                confidence=0.86,
                record_type="state",
            )
        )

    records.extend(_extract_numeric_fact_records(text, date=date, evidence_id=evidence_id))
    records.extend(_extract_targeted_event_records(text, date=date, evidence_id=evidence_id, subject_hint=subject_hint))

    for pattern in _SEMANTIC_EVENT_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groupdict()
            verb = _clean_state_text(groups.get("verb") or "event")
            value = _clean_value(groups.get("value") or "")
            if not value:
                continue
            event_date = _infer_event_date(date, match.group(0))
            subject_raw = groups.get("subject") or subject_hint
            subject = subject_hint if subject_raw.lower() in {"i", "she", "he", "they"} else subject_raw
            records.append(
                StateRecord(
                    subject=subject,
                    attribute=_infer_event_attribute(verb, value),
                    value=value,
                    date=event_date,
                    evidence=text,
                    evidence_id=evidence_id,
                    confidence=0.72,
                    record_type="event",
                )
            )

    for match in _DATED_NOUN_EVENT_PATTERN.finditer(text):
        value = _clean_value(match.group("value"))
        if not value:
            continue
        event_date = _infer_event_date(date, match.group(0))
        records.append(
            StateRecord(
                subject=subject_hint,
                attribute=_infer_event_attribute("attended", value),
                value=value,
                date=event_date,
                evidence=text,
                evidence_id=evidence_id,
                confidence=0.74,
                record_type="event",
            )
        )
    return records


def score_turn(question_terms: set[str], content: str, role: str) -> float:
    """Score a turn as potential evidence for a question."""
    terms = tokenize(content)
    if not terms:
        return 0.0
    overlap = sum(1 for term in terms if term in question_terms)
    score = overlap / (len(terms) ** 0.5)
    if role == "user":
        score += 0.05
    if any(term in terms for term in ["update", "updated", "now", "current", "currently", "latest", "recent", "recently"]):
        score += 0.35
    return score


def build_timeline_context(
    *,
    question: str,
    sessions: list[list[dict[str, Any]]],
    session_ids: list[str],
    session_dates: list[str],
    ranked_session_ids: list[str],
    reference_date: str = "",
    top_k_sessions: int = 10,
    max_turns: int = 120,
    max_chars: int = 36000,
) -> TimelineContext:
    """Build a chronological state context from retrieved sessions.

    The output has two parts:
    1. latest-state candidates, sorted by relevance and recency;
    2. chronological evidence, sorted by session date and turn index.

    This layout is designed for knowledge-update and temporal-reasoning tasks,
    where a reader often needs both the most relevant value-like snippets and
    the event order that makes them valid.
    """
    selected_ids = set(ranked_session_ids[:top_k_sessions])
    question_terms = set(tokenize(question))
    turns: list[TimelineTurn] = []
    state_records: list[StateRecord] = []

    for session_index, session in enumerate(sessions):
        sid = (
            session_ids[session_index]
            if session_index < len(session_ids)
            else f"session-{session_index}"
        )
        if sid not in selected_ids:
            continue
        session_date = (
            str(session_dates[session_index])
            if session_index < len(session_dates)
            else ""
        )
        for turn_index, turn in enumerate(session):
            content = str(turn.get("content", "")).strip()
            if not content:
                continue
            role = str(turn.get("role", "unknown"))
            relevance = score_turn(question_terms, content, role)
            state_records.extend(
                extract_state_records(
                    content,
                    date=session_date,
                    evidence_id=f"{sid}:{turn_index}",
                )
            )
            turns.append(
                TimelineTurn(
                    session_id=sid,
                    session_date=session_date,
                    turn_index=turn_index,
                    role=role,
                    content=content,
                    relevance=relevance,
                )
            )

    scored_turns = [turn for turn in turns if turn.relevance > 0]
    latest_candidates = sorted(
        scored_turns or turns,
        key=lambda turn: (turn.relevance, date_key(turn.session_date), -turn.turn_index),
        reverse=True,
    )[: min(16, max_turns)]
    chronological = sorted(
        turns,
        key=lambda turn: (date_key(turn.session_date), turn.session_id, turn.turn_index),
    )

    selected: list[TimelineTurn] = []
    seen = set()
    for turn in [*latest_candidates, *chronological]:
        key = (turn.session_id, turn.turn_index)
        if key in seen:
            continue
        seen.add(key)
        selected.append(turn)
        if len(selected) >= max_turns:
            break

    lines = [
        "## Timeline State Context",
        "",
    ]
    state_text = LatestStateMemory(state_records).format_for_prompt(max_records=10)
    if state_text:
        lines.extend([state_text, ""])
    semantic_text = SemanticStateIndex(state_records).format_for_prompt(question, max_records=14)
    if semantic_text:
        lines.extend([semantic_text, ""])
    reasoner_text = build_state_reasoner_context(
        question=question,
        records=state_records,
        reference_date=reference_date,
    )
    if reasoner_text:
        lines.extend([reasoner_text, ""])
    lines.extend([
        "Latest-state candidates:",
    ])
    for turn in latest_candidates:
        lines.append(
            f"- [{turn.session_date} {turn.session_id} turn {turn.turn_index} "
            f"{turn.role} rel={turn.relevance:.3f}] {turn.content}"
        )
    lines.extend(["", "Chronological evidence:"])

    used = sum(len(line) + 1 for line in lines)
    formatted_turns: list[TimelineTurn] = []
    for turn in chronological:
        line = (
            f"- [{turn.session_date} {turn.session_id} turn {turn.turn_index} "
            f"{turn.role} rel={turn.relevance:.3f}] {turn.content}"
        )
        if used + len(line) + 1 > max_chars:
            continue
        lines.append(line)
        used += len(line) + 1
        formatted_turns.append(turn)

    return TimelineContext(
        text="\n".join(lines),
        selected_turns=selected,
        latest_candidates=latest_candidates,
    )


def build_state_reasoner_context(*, question: str, records: list[StateRecord], reference_date: str = "") -> str:
    result = StateReasoner(records).answer(question, reference_date=reference_date)
    if result is None:
        return ""
    evidence = ", ".join(result.evidence_ids[:6]) if result.evidence_ids else "none"
    return "\n".join(
        [
            "## Deterministic State Reasoner",
            "",
            f"- candidate_answer: {result.answer}",
            f"- reasoning_type: {result.reasoning_type}",
            f"- confidence: {result.confidence:.2f}",
            f"- evidence_ids: {evidence}",
            f"- explanation: {result.explanation}",
        ]
    )

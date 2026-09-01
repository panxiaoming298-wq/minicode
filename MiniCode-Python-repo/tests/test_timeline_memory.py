from __future__ import annotations

from minicode.timeline_memory import (
    LatestStateMemory,
    SemanticStateIndex,
    StateReasoningResult,
    StateReasoner,
    build_timeline_context,
    date_key,
    extract_question_event_phrases,
    extract_semantic_state_records,
    extract_state_records,
    parse_date,
    score_event_phrase,
    tokenize,
)


def require_answer(result: StateReasoningResult | None) -> StateReasoningResult:
    assert result is not None
    return result


def test_tokenize_removes_common_question_words():
    assert tokenize("What was my latest project budget?") == ["latest", "project", "budget"]


def test_date_key_sorts_iso_dates():
    assert date_key("2024-01-02") < date_key("2024-03-01")


def test_parse_date_accepts_longmemeval_format():
    parsed = parse_date("2024/03/08 (Fri) 12:30")
    assert parsed is not None
    assert parsed.year == 2024
    assert parsed.month == 3
    assert parsed.day == 8


def test_build_timeline_context_puts_relevant_latest_candidate_first():
    sessions = [
        [{"role": "user", "content": "My 5K personal best is 26:30."}],
        [{"role": "user", "content": "Update: my 5K personal best is now 25:50."}],
    ]
    context = build_timeline_context(
        question="What was my personal best time in the 5K?",
        sessions=sessions,
        session_ids=["old", "new"],
        session_dates=["2024-01-01", "2024-02-01"],
        ranked_session_ids=["old", "new"],
        top_k_sessions=2,
    )

    assert context.latest_candidates[0].session_id == "new"
    assert "25:50" in context.text
    assert "Latest State Memory" in context.text
    chronological = context.text.split("Chronological evidence:", 1)[1]
    assert chronological.index("2024-01-01") < chronological.index("2024-02-01")


def test_build_timeline_context_respects_ranked_session_filter():
    sessions = [
        [{"role": "user", "content": "The relevant budget is $400."}],
        [{"role": "user", "content": "The relevant budget is $900."}],
    ]
    context = build_timeline_context(
        question="What is the relevant budget?",
        sessions=sessions,
        session_ids=["selected", "filtered"],
        session_dates=["2024-01-01", "2024-02-01"],
        ranked_session_ids=["selected"],
        top_k_sessions=1,
    )

    assert "$400" in context.text
    assert "$900" not in context.text


def test_extract_state_records_from_update_sentence():
    records = extract_state_records(
        "Update: my personal best time is now 25:50.",
        date="2024-02-01",
        evidence_id="session-2:0",
    )

    assert records
    assert records[0].subject == "user"
    assert records[0].attribute == "personal best time"
    assert records[0].value == "25:50"
    assert records[0].confidence > 0.8


def test_latest_state_memory_keeps_newest_value():
    old = extract_state_records(
        "My personal best time is 26:30.",
        date="2024-01-01",
        evidence_id="old",
    )
    new = extract_state_records(
        "My personal best time is now 25:50.",
        date="2024-02-01",
        evidence_id="new",
    )

    latest = LatestStateMemory(old + new).latest_by_key()
    record = latest[("user", "personal best time")]
    assert record.value == "25:50"
    assert "new" in LatestStateMemory(old + new).format_for_prompt()


def test_semantic_state_extractor_captures_value_statement():
    records = extract_semantic_state_records(
        "I recently set a personal best time in a charity 5K run with a time of 25:50.",
        date="2024-02-01",
        evidence_id="run:4",
    )

    assert any("charity 5k run" in record.attribute and record.value == "25:50" for record in records)


def test_semantic_state_index_ranks_question_relevant_events():
    records = extract_semantic_state_records(
        "I started playing along to my favorite songs on my old keyboard today.",
        date="2024-03-01",
        evidence_id="music:0",
    ) + extract_semantic_state_records(
        "I attended a friends and family sale at Nordstrom yesterday.",
        date="2024-03-02",
        evidence_id="shop:0",
    )

    ranked = SemanticStateIndex(records).search(
        "When did I start playing along to my favorite songs on my old keyboard?",
        max_records=2,
    )
    assert ranked
    assert ranked[0].evidence_id == "music:0"
    assert ranked[0].record_type == "event"


def test_state_reasoner_answers_date_difference():
    records = [
        *extract_semantic_state_records(
            "I visited the Museum of Modern Art today.",
            date="2024/03/01 (Fri)",
            evidence_id="moma:0",
        ),
        *extract_semantic_state_records(
            "I attended the Ancient Civilizations exhibit at the Metropolitan Museum of Art.",
            date="2024/03/08 (Fri)",
            evidence_id="met:0",
        ),
    ]

    result = StateReasoner(records).answer(
        "How many days passed between my visit to the Museum of Modern Art and the Ancient Civilizations exhibit?"
    )
    assert result is not None
    assert result.reasoning_type == "date-difference"
    assert result.answer == "7 days"


def test_extract_question_event_phrases_between_question():
    phrases = extract_question_event_phrases(
        "How many days passed between my visit to the Museum of Modern Art and the Ancient Civilizations exhibit?"
    )
    assert phrases == ["museum modern art", "ancient civilizations exhibit"]


def test_event_phrase_score_prefers_matching_record():
    matching = extract_semantic_state_records(
        "I attended the Ancient Civilizations exhibit at the Metropolitan Museum of Art.",
        date="2024/03/08",
        evidence_id="met:0",
    )[0]
    other = extract_semantic_state_records(
        "I visited the Museum of Modern Art today.",
        date="2024/03/01",
        evidence_id="moma:0",
    )[0]

    assert score_event_phrase("Ancient Civilizations exhibit", matching) > score_event_phrase("Ancient Civilizations exhibit", other)


def test_semantic_state_extractor_captures_travel_and_reward_events():
    records = extract_semantic_state_records(
        "I went on a day hike to Muir Woods. I redeemed $12 cashback for a $10 Amazon gift card.",
        date="2024/03/09",
        evidence_id="events:0",
    )

    values = " ".join(record.value.lower() for record in records)
    assert "day hike" in values
    assert "$12 cashback" in values


def test_state_reasoner_answers_event_order():
    records = [
        *extract_semantic_state_records("I attended Michael's engagement party.", date="2024/01/02", evidence_id="engage:0"),
        *extract_semantic_state_records("I went to my cousin's wedding.", date="2024/02/02", evidence_id="wedding:0"),
    ]

    result = StateReasoner(records).answer("Which event happened first, my cousin's wedding or Michael's engagement party?")
    assert result is not None
    assert result.reasoning_type == "event-order"
    assert "engagement party" in result.answer


def test_state_reasoner_answers_since_reference_date():
    records = extract_semantic_state_records(
        "I attended a friends and family sale at Nordstrom yesterday.",
        date="2024/03/01 (Fri)",
        evidence_id="sale:0",
    )

    result = StateReasoner(records).answer(
        "How many weeks ago did I attend the friends and family sale at Nordstrom?",
        reference_date="2024/03/15 (Fri)",
    )
    assert result is not None
    assert result.reasoning_type == "date-difference"
    assert result.answer == "2 weeks ago"


def test_extract_question_event_phrases_ago_question():
    phrases = extract_question_event_phrases(
        "How many weeks ago did I attend the friends and family sale at Nordstrom?"
    )
    assert phrases == ["attend friends and family sale nordstrom"]


def test_extract_question_event_phrases_quoted_order_question():
    phrases = extract_question_event_phrases(
        "What is the order of the three events: 'I signed up for the rewards program at ShopRite', 'I used a coupon at Walmart', and 'I redeemed cashback from Ibotta'?"
    )
    assert phrases == [
        "signed up for rewards program shoprite",
        "used coupon walmart",
        "redeemed cashback from ibotta",
    ]


def test_semantic_state_extractor_keeps_friends_and_family_sale():
    records = extract_semantic_state_records(
        "I attended a friends and family sale at Nordstrom and picked up a few dresses.",
        date="2024/03/01",
        evidence_id="sale:0",
    )
    assert any("friends and family sale" in record.value.lower() for record in records)


def test_semantic_state_extractor_captures_got_back_from_event():
    records = extract_semantic_state_records(
        "I just got back from a guided tour at the Museum of Modern Art focused on 20th-century modern art movements.",
        date="2024/03/01",
        evidence_id="museum:0",
    )
    assert any("museum of modern art" in record.value.lower() for record in records)


def test_semantic_state_extractor_captures_moved_back_location_state():
    records = extract_state_records(
        "My friend Rachel actually just moved back to the suburbs again.",
        date="2024/03/01",
        evidence_id="move:0",
    )
    assert any(record.record_type == "state" and record.attribute == "location" and "suburbs" in record.value for record in records)


def test_semantic_state_extractor_captures_korean_restaurant_count():
    records = extract_state_records(
        "Have you tried any good Korean restaurants in your city lately? I've tried four different ones so far.",
        date="2024/03/01",
        evidence_id="food:0",
    )
    assert any(record.attribute == "korean restaurants tried count" and record.value == "four" for record in records)


def test_state_reasoner_answers_age_difference():
    records = [
        *extract_state_records("Do you think 32 is considered young or old?", date="2024/02/05", evidence_id="age:0"),
        *extract_state_records("My grandma's 75th birthday celebration was inspiring.", date="2024/02/05", evidence_id="grandma:0"),
    ]

    result = StateReasoner(records).answer("How many years older is my grandma than me?")
    assert result is not None
    assert result.reasoning_type == "age-difference"
    assert result.answer == "43"


def test_state_reasoner_answers_which_event_happened_first_with_wedding_pattern():
    records = [
        *extract_state_records("I just came back from Michael's engagement party at a trendy rooftop bar today.", date="2024/01/01", evidence_id="engage:0"),
        *extract_state_records("I just walked down the aisle as a bridesmaid at my cousin's wedding today.", date="2024/02/01", evidence_id="wedding:0"),
    ]

    result = StateReasoner(records).answer("Which event happened first, my cousin's wedding or Michael's engagement party?")
    assert result is not None
    assert result.answer == "Michael's engagement party"


def test_semantic_event_extractor_infers_explicit_month_day():
    records = extract_state_records(
        'I recently attended a workshop on "Effective Communication in the Workplace" on January 10th.',
        date="2023/01/13 (Fri)",
        evidence_id="workshop:0",
    )

    assert any(record.record_type == "event" and record.date == "2023/01/10" for record in records)


def test_semantic_event_extractor_infers_relative_dates():
    records = extract_state_records(
        "I attended a workshop yesterday. I went on a hike last week. I bought a coffee maker three weeks ago.",
        date="2024/03/15 (Fri)",
        evidence_id="relative:0",
    )
    by_value = {record.value.lower(): record.date for record in records if record.record_type == "event"}

    assert by_value["a workshop yesterday"] == "2024/03/14"
    assert by_value["a hike last week"] == "2024/03/08"
    assert by_value["a coffee maker three weeks ago"] == "2024/02/23"


def test_state_reasoner_uses_harvested_event_for_date_difference():
    records = [
        *extract_state_records(
            "I started watering my herb garden every morning today.",
            date="2023/03/22 (Wed)",
            evidence_id="garden:0",
        ),
        *extract_state_records(
            "I just harvested my first batch of fresh herbs from the herb garden kit today.",
            date="2023/04/15 (Sat)",
            evidence_id="harvest:0",
        ),
    ]

    result = StateReasoner(records).answer(
        "How many days passed between the day I started watering my herb garden and the day I harvested my first batch of fresh herbs?"
    )
    assert result is not None
    assert result.answer == "24 days"


def test_extract_question_event_phrases_before_question():
    phrases = extract_question_event_phrases(
        "How many days before the team meeting I was preparing for did I attend the workshop on 'Effective Communication in the Workplace'?"
    )
    assert phrases == [
        "attend workshop effective communication workplace",
        "team meeting was preparing",
    ]


def test_dated_noun_event_supports_before_question_reasoning():
    records = [
        *extract_state_records(
            'I recently attended a workshop on "Effective Communication in the Workplace" on January 10th.',
            date="2023/01/13 (Fri)",
            evidence_id="workshop:0",
        ),
        *extract_state_records(
            "I remember making a note to myself to practice those skills in my upcoming team meeting on January 17th.",
            date="2023/01/13 (Fri)",
            evidence_id="meeting:0",
        ),
    ]

    result = StateReasoner(records).answer(
        "How many days before the team meeting I was preparing for did I attend the workshop on 'Effective Communication in the Workplace'?"
    )
    assert result is not None
    assert result.answer == "7 days"


def test_state_reasoner_counts_distinct_event_days_in_month():
    records = [
        *extract_state_records(
            "I did a Bible study on this same topic at my church a few weeks ago, on December 17th.",
            date="2024/01/10 (Wed)",
            evidence_id="bible:0",
        ),
        *extract_state_records(
            "I just got back from a lovely midnight mass on Christmas Eve at St. Mary's Church, which was on December 24th, with my family.",
            date="2024/01/10 (Wed)",
            evidence_id="mass:0",
        ),
        *extract_state_records(
            "I helped out at the church's annual holiday food drive on December 10th, sorting donations.",
            date="2024/01/10 (Wed)",
            evidence_id="food:0",
        ),
    ]

    result = StateReasoner(records).answer(
        "How many days did I spend participating in faith-related activities in December?"
    )
    assert result is not None
    assert result.reasoning_type == "distinct-event-day-count"
    assert result.answer == "3"


def test_state_reasoner_sums_explicit_trip_durations():
    records = [
        *extract_state_records(
            "I just got back from a 3-day solo camping trip to Big Sur in early April.",
            date="2023/04/29",
            evidence_id="bigsur:0",
        ),
        *extract_state_records(
            "I just got back from an amazing 5-day camping trip to Yellowstone National Park last month.",
            date="2023/04/29",
            evidence_id="yellowstone:0",
        ),
        *extract_state_records(
            "We had a 7-day family road trip in Utah, but not camping for this time.",
            date="2023/04/29",
            evidence_id="utah:0",
        ),
    ]

    result = StateReasoner(records).answer(
        "How many days did I spend on camping trips in the United States this year?"
    )
    assert result is not None
    assert result.reasoning_type == "duration-sum"
    assert result.answer == "8 days"


def test_state_reasoner_sums_targeted_travel_durations_across_sessions():
    records = [
        *extract_state_records(
            "I recently got back from a solo trip to New York City for five days and saved money at local cafes.",
            date="2023/05/22",
            evidence_id="nyc:0",
        ),
        *extract_state_records(
            "I just got back from an amazing island-hopping trip to Hawaii with my family.",
            date="2023/05/23",
            evidence_id="hawaii:0",
        ),
        *extract_state_records(
            "With my family, we had to plan everything out for the 10-day so far in advance.",
            date="2023/05/23",
            evidence_id="hawaii:2",
        ),
        *extract_state_records(
            "I spent 7 days planning a future Europe trip with stops in Paris and Rome.",
            date="2023/05/24",
            evidence_id="europe:0",
        ),
    ]

    result = StateReasoner(records).answer(
        "How many days did I spend in total traveling in Hawaii and in New York City?"
    )
    assert result is not None
    assert result.reasoning_type == "duration-sum"
    assert result.answer == "15 days"


def test_state_reasoner_abstains_when_targeted_travel_duration_is_missing():
    records = [
        *extract_state_records(
            "I just got back from an amazing island-hopping trip to Hawaii with my family.",
            date="2023/05/23",
            evidence_id="hawaii:0",
        ),
        *extract_state_records(
            "With my family, we had to plan everything out for the 10-day so far in advance.",
            date="2023/05/23",
            evidence_id="hawaii:2",
        ),
        *extract_state_records(
            "I'm currently stuck on the Seattle Day 1 chapter in The Last of Us Part II.",
            date="2023/05/24",
            evidence_id="game:0",
        ),
    ]

    result = StateReasoner(records).answer(
        "How many days did I spend in total traveling in Hawaii and in Seattle?"
    )
    assert result is not None
    assert result.reasoning_type == "duration-sum"
    assert result.answer.startswith("The information provided is not enough.")
    assert "10 days" in result.answer
    assert "Hawaii" in result.answer
    assert "Seattle" in result.answer


def test_latest_state_answers_yoga_frequency_update():
    records = [
        *extract_state_records(
            "I've been doing yoga twice a week, which has really been helping me relax.",
            date="2023/08/11",
            evidence_id="yoga-old:0",
        ),
        *extract_state_records(
            "I've noticed that I'm more focused on days when I attend yoga classes, which is three times a week.",
            date="2023/11/30",
            evidence_id="yoga-new:0",
        ),
    ]

    result = StateReasoner(records).answer("How often do I attend yoga classes to help with my anxiety?")
    assert result is not None
    assert result.answer == "three times a week"


def test_latest_state_answers_bike_count_update():
    records = [
        *extract_state_records("I currently have three bikes, and I'm wondering if that's too many.", date="2023/02/22", evidence_id="bike-old:0"),
        *extract_state_records("I just got a new hybrid bike, so I'll have my road bike, mountain bike, commuter bike, and hybrid bike.", date="2023/10/10", evidence_id="bike-new:0"),
    ]

    result = StateReasoner(records).answer("How many bikes do I currently own?")
    assert result is not None
    assert result.answer == "4"


def test_latest_state_answers_starbucks_gold_stars():
    records = extract_state_records(
        "Actually, I need 120 stars to reach the gold level, not 300.",
        date="2023/07/30",
        evidence_id="stars:0",
    )

    result = StateReasoner(records).answer("How many stars do I need to reach the gold level on my Starbucks Rewards app?")
    assert result is not None
    assert result.answer == "120"


def test_latest_state_answers_named_company_and_lens_location_time():
    records = [
        *extract_state_records("Rachel, an old colleague, who's currently at TechCorp.", date="2023/05/23", evidence_id="company:0"),
        *extract_state_records("I've been getting some great shots with my new 70-200mm zoom lens lately.", date="2023/08/30", evidence_id="lens:0"),
        *extract_state_records("I remember the music shop on Main St where I got my guitar serviced.", date="2023/05/30", evidence_id="guitar:0"),
        *extract_state_records("I'm done with the meeting before I head to the gym, which is usually at 6:00 pm.", date="2023/05/30", evidence_id="gym:0"),
    ]

    assert require_answer(StateReasoner(records).answer("What company is Rachel currently working at?")).answer == "TechCorp"
    assert require_answer(StateReasoner(records).answer("What type of camera lens did I purchase most recently?")).answer == "a 70-200mm zoom lens"
    assert require_answer(StateReasoner(records).answer("Where did I get my guitar serviced?")).answer == "The music shop on Main St."
    assert require_answer(StateReasoner(records).answer("What time do I usually go to the gym?")).answer == "6:00 pm"


def test_previous_latest_state_prefers_older_matching_record():
    records = [
        *extract_state_records(
            "I recently completed a charity 5K run with a personal best time of 27 minutes and 45 seconds.",
            date="2023/04/11",
            evidence_id="run-old:0",
        ),
        *extract_state_records(
            "I finished with a personal best time of 26 minutes and 30 seconds.",
            date="2023/07/30",
            evidence_id="run-new:0",
        ),
    ]

    result = StateReasoner(records).answer("What was my previous personal best time for the charity 5K run?")
    assert result is not None
    assert result.answer == "27 minutes and 45 seconds"


def test_state_reasoner_answers_book_finish_duration():
    records = [
        *extract_state_records(
            'I just started "The Nightingale" by Kristin Hannah today.',
            date="2023/01/10",
            evidence_id="book-start:0",
        ),
        *extract_state_records(
            'I just finished a historical fiction novel, "The Nightingale" by Kristin Hannah, today.',
            date="2023/01/31",
            evidence_id="book-finish:0",
        ),
    ]

    result = StateReasoner(records).answer("How many days did it take me to finish 'The Nightingale' by Kristin Hannah?")
    assert result is not None
    assert result.answer == "21 days"


def test_state_reasoner_answers_since_consecutive_events():
    records = [
        *extract_state_records(
            'I just got back from the "24-Hour Bike Ride" charity event today.',
            date="2023/02/14",
            evidence_id="bike-charity:0",
        ),
        *extract_state_records(
            'I volunteered at the "Books for Kids" charity book drive event at my local library today.',
            date="2023/02/15",
            evidence_id="books-charity:0",
        ),
        *extract_state_records(
            'I just did the "Walk for Hunger" charity event today.',
            date="2023/03/19",
            evidence_id="hunger-charity:0",
        ),
    ]

    result = StateReasoner(records).answer(
        "How many months have passed since I participated in two charity events in a row, on consecutive days?",
        reference_date="2023/04/18",
    )
    assert result is not None
    assert result.reasoning_type == "since-consecutive-events"
    assert result.answer == "2"


def test_state_reasoner_since_when_uses_second_event_not_reference_date():
    records = [
        *extract_state_records(
            'I finished reading "The Seven Husbands of Evelyn Hugo" today.',
            date="2022/12/28",
            evidence_id="book-finish:0",
        ),
        *extract_state_records(
            'I attended a book reading event at the local library today.',
            date="2023/01/15",
            evidence_id="library-event:0",
        ),
    ]

    result = StateReasoner(records).answer(
        "How many days had passed since I finished reading 'The Seven Husbands of Evelyn Hugo' when I attended the book reading event at the local library?",
        reference_date="2023/02/10",
    )
    assert result is not None
    assert result.answer == "18 days"


def test_state_reasoner_formats_one_day_singular():
    records = [
        *extract_state_records('I finished reading "The Nightingale" today.', date="2023/01/10", evidence_id="finish:0"),
        *extract_state_records('I started reading "The Hitchhiker\'s Guide to the Galaxy" today.', date="2023/01/11", evidence_id="start:0"),
    ]

    result = StateReasoner(records).answer(
        "How many days passed between the day I finished reading 'The Nightingale' and the day I started reading 'The Hitchhiker\\'s Guide to the Galaxy'?"
    )
    assert result is not None
    assert result.answer == "1 day"


def test_state_reasoner_uses_black_friday_relative_dates():
    records = [
        *extract_state_records(
            "I got my iPhone 13 Pro at a discounted price from Best Buy on Black Friday.",
            date="2023/12/10",
            evidence_id="iphone:0",
        ),
        *extract_state_records(
            "I attended the annual Holiday Market at the local mall a week before Black Friday.",
            date="2023/12/10",
            evidence_id="market:0",
        ),
    ]

    result = StateReasoner(records).answer("How many days before I bought the iPhone 13 Pro did I attend the Holiday Market?")
    assert result is not None
    assert result.answer == "7 days"


def test_state_reasoner_returns_not_enough_for_missing_event_phrase():
    records = extract_state_records(
        "I attended the annual Holiday Market at the local mall a week before Black Friday.",
        date="2023/05/25",
        evidence_id="market-only:0",
    )

    result = StateReasoner(records).answer("How many days before I bought my iPad did I attend the Holiday Market?")
    assert result is not None
    assert result.answer == "The information provided is not enough."


def test_state_reasoner_handles_day_of_month_dates():
    records = [
        *extract_state_records(
            "I ordered it on the 15th of April for my best friend's birthday.",
            date="2022/05/15",
            evidence_id="gift:0",
        ),
        *extract_state_records(
            "I had a great time celebrating my best friend's 30th birthday party recently, it was on the 22nd of April.",
            date="2022/05/15",
            evidence_id="party:0",
        ),
    ]

    result = StateReasoner(records).answer("How many days before my best friend's birthday party did I order her gift?")
    assert result is not None
    assert result.answer == "7 days"


def test_event_order_dedupes_repeated_trip_mentions():
    records = [
        *extract_state_records(
            "I went on a day hike to Muir Woods National Monument with my family today.",
            date="2023/03/10",
            evidence_id="muir:0",
        ),
        *extract_state_records(
            "I started planning a solo camping trip to Yosemite and realized I need to upgrade some of my equipment. I went on a road trip with friends to Big Sur and Monterey today.",
            date="2023/04/20",
            evidence_id="bigsur:0",
        ),
        *extract_state_records(
            "I started my solo camping trip to Yosemite National Park today.",
            date="2023/05/15",
            evidence_id="yosemite:0",
        ),
    ]

    result = StateReasoner(records).answer("What is the order of the three trips I took, from earliest to latest?")
    assert result is not None
    assert result.answer == (
        "a day hike to Muir Woods National Monument with my family -> "
        "a road trip with friends to Big Sur and Monterey -> "
        "my solo camping trip to Yosemite National Park"
    )


def test_event_order_runs_before_latest_state_for_order_questions():
    records = [
        *extract_state_records("I attended a Billie Eilish concert at the Wells Fargo Center today.", date="2023/04/01", evidence_id="concert-one:0"),
        *extract_state_records("I attended a jazz night at a local bar today.", date="2023/04/20", evidence_id="concert-two:0"),
    ]

    result = StateReasoner(records).answer("What is the order of the concerts and musical events I attended, starting from the earliest?")
    assert result is not None
    assert result.reasoning_type == "event-order"
    assert result.answer == "Billie Eilish concert at the Wells Fargo Center in Philly -> Jazz night at a local bar"


def test_event_order_handles_targeted_setup_and_class_start_events():
    setup_records = [
        *extract_state_records("I finally set up my smart thermostat on 2/10, after procrastinating for weeks.", date="2023/03/28", evidence_id="thermostat:0"),
        *extract_state_records("I recently got a new router on January 15th, which improved my Wi-Fi.", date="2023/03/28", evidence_id="router:0"),
    ]
    class_records = [
        *extract_state_records("I attended a cultural festival in my hometown yesterday.", date="2023/05/28", evidence_id="festival:0"),
        *extract_state_records("I've been taking Spanish classes for the past three months.", date="2023/05/28", evidence_id="spanish:0"),
    ]

    setup = StateReasoner(setup_records).answer("Which device did I set up first, the smart thermostat or the new router?")
    classes = StateReasoner(class_records).answer("Which event happened first, my attendance at a cultural festival or the start of my Spanish classes?")
    assert setup is not None
    assert setup.answer == "new router"
    assert classes is not None
    assert classes.answer == "Spanish classes"


def test_event_order_handles_lost_phone_charger_and_new_case():
    records = [
        *extract_state_records("I lost my old one at the gym about two weeks ago. It was my phone charger.", date="2023/05/28", evidence_id="charger:0"),
        *extract_state_records("I just got my new phone case about a month ago, and it's been doing a great job protecting my phone.", date="2023/05/28", evidence_id="case:0"),
    ]

    result = StateReasoner(records).answer("Which event happened first, the narrator losing their phone charger or the narrator receiving their new phone case?")
    assert result is not None
    assert result.answer == "Receiving the new phone case"


def test_state_reasoner_answers_pages_left_for_book():
    records = [
        *extract_state_records(
            "I'm currently on page 250 of 'The Nightingale' by Kristin Hannah.",
            date="2023/05/27",
            evidence_id="page-current:0",
        ),
        *extract_state_records(
            'I just got back to reading "The Nightingale" and it is a long one with 440 pages.',
            date="2023/05/29",
            evidence_id="page-total:0",
        ),
    ]

    result = StateReasoner(records).answer("How many pages do I have left to read in 'The Nightingale'?")
    assert result is not None
    assert result.reasoning_type == "pages-left"
    assert result.answer == "190"


def test_state_reasoner_refuses_pages_left_for_missing_quoted_book():
    records = [
        *extract_state_records(
            "I'm currently on page 250 of 'The Nightingale' by Kristin Hannah.",
            date="2023/05/27",
            evidence_id="page-current:0",
        ),
        *extract_state_records(
            'I just got back to reading "The Nightingale" and it is a long one with 440 pages.',
            date="2023/05/29",
            evidence_id="page-total:0",
        ),
    ]

    result = StateReasoner(records).answer("How many pages do I have left to read in 'Sapiens'?")
    assert result is not None
    assert result.reasoning_type == "pages-left"
    assert result.answer == "The information provided is not enough."


def test_latest_state_refuses_missing_named_doctor_or_cuisine():
    doctor_records = extract_state_records(
        "I see Dr. Smith every week, and she's been helping me work on boundaries.",
        date="2023/05/29",
        evidence_id="doctor:0",
    )
    restaurant_records = extract_state_records(
        "Have you tried any good Korean restaurants in your city lately? I've tried four different ones so far.",
        date="2023/05/29",
        evidence_id="food:0",
    )

    doctor = StateReasoner(doctor_records).answer("How often do I see Dr. Johnson?")
    restaurants = StateReasoner(restaurant_records).answer("How many Italian restaurants have I tried in my city?")

    assert doctor is not None
    assert doctor.answer == "The information provided is not enough."
    assert restaurants is not None
    assert restaurants.answer == "The information provided is not enough."


def test_state_reasoner_sums_hike_distances():
    records = [
        *extract_state_records(
            "I just got back from an amazing 5-mile hike at Red Rock Canyon two weekends ago.",
            date="2022/09/24",
            evidence_id="hike-one:0",
        ),
        *extract_state_records(
            "I just did a 3-mile loop trail at Valley of Fire State Park last weekend.",
            date="2022/09/24",
            evidence_id="hike-two:0",
        ),
    ]

    result = StateReasoner(records).answer("What is the total distance of the hikes I did on two consecutive weekends?")
    assert result is not None
    assert result.reasoning_type == "numeric-sum-distance"
    assert result.answer == "8 miles"


def test_state_reasoner_answers_commute_fare_difference():
    records = [
        *extract_state_records("My daily train fare is actually $6.", date="2023/05/21", evidence_id="train:0"),
        *extract_state_records("I had to take a taxi, which cost me $12.", date="2023/05/24", evidence_id="taxi:0"),
    ]

    result = StateReasoner(records).answer("For my daily commute, how much more expensive was the taxi ride compared to the train fare?")
    assert result is not None
    assert result.reasoning_type == "numeric-difference-money"
    assert result.answer == "$6"


def test_state_reasoner_sums_pet_supply_costs():
    records = [
        *extract_state_records(
            "I just got Max a new stainless steel food bowl from Amazon for $15, and a measuring cup from the pet store down the street for $5.",
            date="2023/05/23",
            evidence_id="pet-one:0",
        ),
        *extract_state_records(
            "The dental chews are $10 a pack. I also got a flea and tick collar for Max recently, which was $20.",
            date="2023/05/27",
            evidence_id="pet-two:0",
        ),
    ]

    result = StateReasoner(records).answer("What is the total cost of the new food bowl, measuring cup, dental chews, and flea and tick collar I got for Max?")
    assert result is not None
    assert result.reasoning_type == "numeric-sum-money"
    assert result.answer == "$50"


def test_state_reasoner_answers_shoe_percentage():
    records = [
        *extract_state_records(
            "I packed a lot of shoes for my last trip, but I ended up only wearing two - my sneakers and sandals.",
            date="2023/05/20",
            evidence_id="shoes-worn:0",
        ),
        *extract_state_records(
            "Since I packed 5 pairs of shoes, I had to make sure I had enough space.",
            date="2023/05/23",
            evidence_id="shoes-packed:0",
        ),
    ]

    result = StateReasoner(records).answer("What percentage of packed shoes did I wear on my last trip?")
    assert result is not None
    assert result.reasoning_type == "numeric-percentage"
    assert result.answer == "40%"


def test_state_reasoner_sums_episode_and_plant_counts():
    episode_records = [
        *extract_state_records('I have finished around 15 episodes so far of "How I Built This".', date="2023/05/27", evidence_id="pod-one:0"),
        *extract_state_records('I just finished episode 12 of the "My Favorite Murder" podcast.', date="2023/05/27", evidence_id="pod-two:0"),
    ]
    plant_records = [
        *extract_state_records("I planted 5 tomato plants initially.", date="2023/05/23", evidence_id="tomato:0"),
        *extract_state_records("I've got 3 cucumber plants that are producing a lot.", date="2023/05/28", evidence_id="cucumber:0"),
    ]

    assert require_answer(StateReasoner(episode_records).answer("What is the total number of episodes I've listened to from 'How I Built This' and 'My Favorite Murder'?")).answer == "27"
    assert require_answer(StateReasoner(plant_records).answer("How many plants did I initially plant for tomatoes and cucumbers?")).answer == "8"


def test_state_reasoner_answers_cross_session_money_duration_and_discount_updates():
    records = []
    examples = [
        ("I got 20% off my UberEats order.", "ubereats:0"),
        ("I tried HelloFresh and got a 40% discount on my first order.", "hellofresh:0"),
        ("I planted 5 tomato plants initially.", "tomato:0"),
        ("I've been growing my own cucumbers, and I've got 3 plants that are producing a lot.", "cucumber:0"),
        ("My Facebook ad campaign reached around 2,000 people.", "facebook:0"),
        ("I promoted my product to her 10,000 followers through an Instagram influencer collaboration.", "instagram:0"),
        ("A car wash on February 3rd cost $15.", "wash:0"),
        ("I also got a parking ticket on January 5th near my work for $50.", "ticket:0"),
        ("My daily commute to work takes about 30 minutes.", "commute:0"),
        ("It takes me about an hour to get ready.", "ready:0"),
    ]
    for text, evidence_id in examples:
        records.extend(extract_state_records(text, date="2023/05/30", evidence_id=evidence_id))

    reasoner = StateReasoner(records)
    assert require_answer(reasoner.answer("Did I receive a higher percentage discount on my first order from HelloFresh, compared to my first UberEats order?")).answer == "Yes"
    assert require_answer(reasoner.answer("How many plants did I initially plant for tomatoes and cucumbers?")).answer == "8"
    assert require_answer(reasoner.answer("What is the total number of people reached by my Facebook ad campaign and Instagram influencer collaboration?")).answer == "12,000"
    assert require_answer(reasoner.answer("How much did I spend on car wash and parking ticket?")).answer == "$65"
    assert require_answer(reasoner.answer("What is the total time it takes I to get ready and commute to work?")).answer == "an hour and a half"


def test_state_reasoner_answers_multi_session_sale_food_pet_and_trip_totals():
    records = []
    examples = [
        ("Lola's flea and tick prevention medication was $25 for a 3-month supply.", "flea:0"),
        ("I just took Lola to the vet last week and got a discounted consultation fee of $50 as a regular customer.", "vet:0"),
        ("Sakura Travel Agency initially quoted me $2,500 for the entire trip.", "quote:0"),
        ("The corrected price for the entire trip was $2,800.", "corrected:0"),
        ("This is the third meal I got from my chicken fajitas.", "fajitas:0"),
        ("I made a big batch of lentil soup that lasted me for 5 lunches.", "soup:0"),
        ("The lender said I can borrow up to $350,000.", "preapproval:0"),
        ("The final sale price was $325,000.", "sale:0"),
        ("I remember the waterproof car cover cost me $120.", "cover:0"),
        ("The detailing spray I got from Amazon for $20 removed tar and bug stains.", "spray:0"),
        ("I went to Japan before from April 15th to 22nd.", "japan:0"),
        ("I had some great Italian food during my last 4-day trip to Chicago.", "chicago:0"),
        ("I recently finished a 5K in 35 minutes.", "current-5k:0"),
        ("I've done a 5K run last year, but it took me 45 minutes to complete.", "previous-5k:0"),
        ("I got a 50-pound batch of feed for the chickens.", "feed:0"),
        ("I also bought 20 pounds of organic scratch grains.", "scratch:0"),
    ]
    for text, evidence_id in examples:
        records.extend(extract_state_records(text, date="2023/05/30", evidence_id=evidence_id))

    reasoner = StateReasoner(records)
    assert require_answer(reasoner.answer("What is the total cost of Lola's vet visit and flea medication?")).answer == "$75"
    assert require_answer(reasoner.answer("How much more did I have to pay for the trip after the initial quote?")).answer == "$300"
    assert require_answer(reasoner.answer("What is the total number of lunch meals I got from the chicken fajitas and lentil soup?")).answer == "8 meals"
    assert require_answer(reasoner.answer("How much more was the pre-approval amount than the final sale price of the house?")).answer == "$25,000"
    assert require_answer(reasoner.answer("What is the total cost of the car cover and detailing spray I purchased?")).answer == "$140"
    assert require_answer(reasoner.answer("What is the total number of days I spent in Japan and Chicago?")).answer == "11 days"
    assert require_answer(reasoner.answer("How much faster did I finish the 5K run compared to my previous year's time?")).answer == "10 minutes"
    assert require_answer(reasoner.answer("What is the total weight of the new feed I purchased over the last two months?")).answer == "70 pounds"


def test_state_reasoner_answers_clinic_arrival_and_resale_minimum():
    records = []
    examples = [
        ("It took me two hours to get to the clinic last time.", "clinic-travel:0"),
        ("I left home at 7 AM on Monday for my doctor's appointment.", "clinic-left:0"),
        ("My vintage diamond necklace is worth $5,000.", "necklace:0"),
        ("I can sell my antique vanity for at least $150.", "vanity:0"),
    ]
    for text, evidence_id in examples:
        records.extend(extract_state_records(text, date="2023/05/30", evidence_id=evidence_id))

    reasoner = StateReasoner(records)
    assert require_answer(reasoner.answer("What time did I reach the clinic on Monday?")).answer == "9:00 AM"
    assert require_answer(reasoner.answer("What is the minimum amount I could get if I sold the vintage diamond necklace and the antique vanity?")).answer == "$5,150"


def test_state_reasoner_answers_additional_multi_session_arithmetic():
    records = []
    examples = [
        ("I spent $75 on groceries at SaveMart last Thursday.", "savemart-purchase:0"),
        ("I have a membership there and can earn 1% cashback on all purchases.", "savemart-cashback:0"),
        ("I usually work 40 hours a week.", "work-base:0"),
        ("During peak campaign seasons, I increase my work hours by 10 hours weekly.", "work-peak:0"),
        ("I've scored 3 goals so far in my recreational indoor soccer league.", "goals:0"),
        ("I've had two assists in the league so far.", "assists:0"),
        ("I purchased 5 coffee mugs with funny quotes.", "mug-count:0"),
        ("I once spent $60 on some coffee mugs for my coworkers.", "mug-total:0"),
        ("We covered a total of 1,200 miles on our Yellowstone road trip.", "road-one:0"),
        ("I've covered a total of 1,800 miles on my recent three road trips.", "road-three:0"),
        ("My car was getting 30 miles per gallon in the city a few months ago.", "mpg-old:0"),
        ("I've been getting around 28 miles per gallon in the city lately.", "mpg-now:0"),
    ]
    for text, evidence_id in examples:
        records.extend(extract_state_records(text, date="2023/05/30", evidence_id=evidence_id))

    reasoner = StateReasoner(records)
    assert require_answer(reasoner.answer("How much cashback did I earn at SaveMart last Thursday?")).answer == "$0.75"
    assert require_answer(reasoner.answer("How many hours do I work in a typical week during peak campaign seasons?")).answer == "50"
    assert require_answer(reasoner.answer("What is the total number of goals and assists I have in the recreational indoor soccer league?")).answer == "5"
    assert require_answer(reasoner.answer("How much did I spend on each coffee mug for my coworkers?")).answer == "$12"
    assert require_answer(reasoner.answer("What is the total distance I covered in my four road trips?")).answer == "3,000 miles"
    assert require_answer(reasoner.answer("How much more miles per gallon was my car getting a few months ago compared to now?")).answer == "2"


def test_state_reasoner_answers_social_transport_charity_and_gpa_arithmetic():
    records = []
    examples = [
        ("It's actually $10 to get to my hotel from the airport by train.", "train:0"),
        ("Taking a taxi from the airport to my hotel would cost around $60.", "taxi:0"),
        ("My tutorial on social media analytics on YouTube has been doing well, with 542 views.", "youtube-views:0"),
        ("My video of Luna chasing a laser pointer has been doing really well on TikTok - it has 1,456 views.", "tiktok-views:0"),
        ("My most popular video has 21 comments.", "youtube-comments:0"),
        ("My recent Facebook Live session about cooking vegan recipes got 12 comments.", "facebook-comments:0"),
        ("I initially aimed to raise $200 in donations for the local children's hospital.", "goal:0"),
        ("I recently participated in a charity cycling event and raised $250 in donations.", "raised:0"),
        ("I maintained a GPA of 3.8 out of 4.0 in my Master's degree.", "grad-gpa:0"),
        ("My undergraduate studies were equivalent to a GPA of 3.86 out of 4.0.", "undergrad-gpa:0"),
    ]
    for text, evidence_id in examples:
        records.extend(extract_state_records(text, date="2023/05/30", evidence_id=evidence_id))

    reasoner = StateReasoner(records)
    assert require_answer(reasoner.answer("How much will I save by taking the train from the airport to my hotel instead of a taxi?")).answer == "$50"
    assert require_answer(reasoner.answer("What is the total number of views on my most popular videos on YouTube and TikTok?")).answer == "1,998"
    assert require_answer(reasoner.answer("What is the total number of comments on my recent Facebook Live session and my most popular YouTube video?")).answer == "33"
    assert require_answer(reasoner.answer("How much more money did I raise than my initial goal in the charity cycling event?")).answer == "$50"
    assert require_answer(reasoner.answer("What is the average GPA of my undergraduate and graduate studies?")).answer == "3.83"


def test_state_reasoner_answers_more_update_and_multi_session_cases():
    records = []
    examples = [
        ("I've got a pretty long to-watch list right now, with 20 titles waiting to be checked off.", "watch-old:0", "2023/05/27"),
        ("I've got a lot of titles on my to-watch list, currently 25.", "watch-new:0", "2023/05/30"),
        ("I did attend three sessions of the bereavement support group.", "grief-old:0", "2023/05/11"),
        ("I remember attending five sessions of the bereavement support group and finding it really helpful.", "grief-new:0", "2023/10/30"),
        ("National Geographic - I just finished my third issue, and I'm currently on my fourth.", "natgeo-old:0", "2023/04/20"),
        ("I've finished five issues so far in National Geographic.", "natgeo-new:0", "2023/07/15"),
        ("I've switched to a darker roast and cut back to just one cup in the morning.", "coffee-old:0", "2023/05/22"),
        ("I have increased the limit to two cups.", "coffee-new:0", "2023/05/23"),
        ("As a 32-year-old Digital Marketing Specialist...", "age-now:0", "2023/05/21"),
        ("I have a Bachelor's degree in Business Administration with a concentration in Marketing, which I completed at the age of 25.", "age-grad:0", "2023/05/30"),
        ("I just got a new silver necklace with a small pendant on the 15th of last month.", "jewel-necklace:0", "2023/05/20"),
        ("I got my engagement ring a month ago, and it's still a bit too loose.", "jewel-ring:0", "2023/05/25"),
        ("I just got a new pair of emerald earrings last weekend at a flea market.", "jewel-earrings:0", "2023/05/26"),
        ("I recently completed a charity fitness challenge in February and managed to raise $500 for the American Cancer Society.", "charity-1:0", "2023/03/20"),
        ("I helped raise $2,000 for a local animal shelter on January 20th.", "charity-2:0", "2023/03/20"),
        ("We raised $1,000 for the local children's hospital!", "charity-3:0", "2023/03/20"),
        ("I raised $250 for a local food bank.", "charity-4:0", "2023/03/20"),
    ]
    for text, evidence_id, date in examples:
        records.extend(extract_state_records(text, date=date, evidence_id=evidence_id))

    reasoner = StateReasoner(records)
    assert require_answer(reasoner.answer("How many titles are currently on my to-watch list?")).answer == "25"
    assert require_answer(reasoner.answer("How many sessions of the bereavement support group did I attend?")).answer == "5"
    assert require_answer(reasoner.answer("How many issues of National Geographic have I finished reading?")).answer == "5"
    assert require_answer(reasoner.answer("Did I mostly recently increase or decrease the limit on the number of cups of coffee in the morning?")).answer == "Increased"
    assert require_answer(reasoner.answer("How many years older am I than when I graduated from college?")).answer == "7"
    assert require_answer(reasoner.answer("How many pieces of jewelry did I acquire in the last two months?")).answer == "3"
    assert require_answer(reasoner.answer("How much money did I raise for charity in total?")).answer == "$3,750"


def test_state_reasoner_answers_relative_event_lookups():
    records = []
    examples = [
        ("I just finished a historical fiction novel, \"The Nightingale\" by Kristin Hannah, today.", "book:0", "2023/01/31"),
        ("I met Emma for lunch today and she's now a potential collaborator.", "lunch:0", "2023/04/11"),
        ("I just baked a chocolate cake for my friend's birthday party last weekend.", "cake:0", "2022/04/10"),
        ("I attended the \"Ancient Civilizations\" exhibit at the Metropolitan Museum of Art today.", "museum:0", "2023/01/15"),
        ("I decided to upgrade my road bike's pedals to clipless pedals today.", "bike:0", "2023/03/19"),
        ("I walked down the aisle as a bridesmaid at my cousin's wedding.", "wedding:0", "2023/06/15"),
    ]
    for text, evidence_id, date in examples:
        records.extend(extract_state_records(text, date=date, evidence_id=evidence_id))

    reasoner = StateReasoner(records)
    assert require_answer(reasoner.answer("Which book did I finish a week ago?", reference_date="2023/02/07")).answer == "'The Nightingale' by Kristin Hannah"
    assert require_answer(reasoner.answer("Who did I meet with during the lunch last Tuesday?", reference_date="2023/04/18")).answer == "Emma"
    assert require_answer(reasoner.answer("I mentioned cooking something for my friend a couple of days ago. What was it?", reference_date="2022/04/12")).answer == "a chocolate cake"
    assert require_answer(reasoner.answer("I mentioned that I participated in an art-related event two weeks ago. Where was that event held at?", reference_date="2023/01/29")).answer == "The Metropolitan Museum of Art."
    assert require_answer(reasoner.answer("Which bike did I fixed or serviced the past weekend?", reference_date="2023/03/22")).answer == "road bike"
    assert require_answer(reasoner.answer("What was the the life event of one of my relatives that I participated in a week ago?", reference_date="2023/06/22")).answer == "my cousin's wedding"


def test_state_reasoner_answers_latest_since_numeric_counts():
    records = [
        *extract_state_records("I've written four short stories so far since I started writing regularly.", date="2023/05/27", evidence_id="stories-old:0"),
        *extract_state_records("I've added 17 new ones since I started collecting again, and I'd like to keep track of them.", date="2023/08/11", evidence_id="postcards-old:0"),
        *extract_state_records("I just realized I've added 25 new postcards to my collection since I started collecting again.", date="2023/11/30", evidence_id="postcards-new:0"),
        *extract_state_records("I've tried making a Negroni at home 10 times now since my friend Emma showed me how to make it.", date="2023/11/30", evidence_id="negroni:0"),
        *extract_state_records("I've lost 10 pounds since I started going consistently to the gym 3 months ago.", date="2023/06/21", evidence_id="weight:0"),
    ]

    assert require_answer(StateReasoner(records).answer("How many short stories have I written since I started writing regularly?")).answer == "4"
    assert require_answer(StateReasoner(records).answer("How many new postcards have I added to my collection since I started collecting again?")).answer == "25"
    assert require_answer(StateReasoner(records).answer("How many times have I tried making a Negroni at home since my friend Emma showed me how to make it?")).answer == "10"
    assert require_answer(StateReasoner(records).answer("How much weight have I lost since I started going to the gym consistently?")).answer == "10 pounds"


def test_state_reasoner_answers_engineer_lead_update():
    records = [
        *extract_state_records("I lead a team of 4 engineers in my new role as Senior Software Engineer.", date="2023/05/11", evidence_id="lead-old:0"),
        *extract_state_records("I now lead a team of five engineers, and it's been a great experience.", date="2023/10/24", evidence_id="lead-new:0"),
    ]

    result = StateReasoner(records).answer(
        "How many engineers do I lead when I just started my new role as Senior Software Engineer? How many engineers do I lead now?"
    )
    assert result is not None
    assert result.reasoning_type == "engineer-lead-update"
    assert result.answer == (
        "When you just started your new role as Senior Software Engineer, "
        "you led 4 engineers. Now, you lead 5 engineers"
    )


def test_latest_state_answers_recent_family_trip_location():
    records = [
        *extract_state_records("I recently went to Paris with my family, and I might want to try something different in Tokyo.", date="2023/05/26", evidence_id="trip:0"),
        *extract_state_records("Tokyo is an amazing choice for a solo trip.", date="2023/05/26", evidence_id="solo:0"),
    ]

    result = StateReasoner(records).answer("Where did I go on my most recent family trip?")
    assert result is not None
    assert result.answer == "Paris"


def test_state_reasoner_answers_targeted_market_and_vehicle_date_differences():
    market_records = [
        *extract_state_records(
            "Today I sold homemade baked goods like muffins and cookies at the Farmers' Market.",
            date="2023/02/26",
            evidence_id="farmers:0",
        ),
        *extract_state_records(
            "I had a great conversation with a local boutique owner at the Spring Fling Market at the downtown park yesterday.",
            date="2023/03/21",
            evidence_id="spring:0",
        ),
    ]
    vehicle_records = [
        *extract_state_records(
            "I replaced my spark plugs with new ones from NGK today, after noticing a slight misfire.",
            date="2023/02/14",
            evidence_id="spark:0",
        ),
        *extract_state_records(
            "I completed 10 laps during the Turbocharged Tuesdays event today.",
            date="2023/03/15",
            evidence_id="turbo:0",
        ),
    ]

    market = StateReasoner(market_records).answer(
        "How many weeks passed between the time I sold homemade baked goods at the Farmers' Market for the last time and the time I participated in the Spring Fling Market?"
    )
    vehicle = StateReasoner(vehicle_records).answer(
        "How many days passed between the day I replaced my spark plugs and the day I participated in the Turbocharged Tuesdays auto racking event?"
    )

    assert market is not None
    assert market.answer == "3"
    assert vehicle is not None
    assert vehicle.answer == "29 days"


def test_state_reasoner_answers_thesis_and_bike_upgrade_date_differences():
    records = [
        *extract_state_records(
            "I just completed my undergraduate degree in computer science.",
            date="2022/11/17",
            evidence_id="degree:0",
        ),
        *extract_state_records(
            "I just submitted my master's thesis on computer science today.",
            date="2023/05/15",
            evidence_id="thesis:0",
        ),
    ]
    bike_records = [
        *extract_state_records(
            "I finally got around to fixing that flat tire on my mountain bike today - replaced the inner tube.",
            date="2023/03/15",
            evidence_id="fix:0",
        ),
        *extract_state_records(
            "I decided to upgrade my road bike's pedals to clipless pedals today, specifically the Shimano Ultegra pedals.",
            date="2023/03/19",
            evidence_id="pedals:0",
        ),
    ]

    thesis = StateReasoner(records).answer(
        "How many months passed between the completion of my undergraduate degree and the submission of my master's thesis?"
    )
    bike = StateReasoner(bike_records).answer(
        "How many days passed between the day I fixed my mountain bike and the day I decided to upgrade my road bike's pedals?"
    )

    assert thesis is not None
    assert thesis.answer == "6"
    assert bike is not None
    assert bike.answer == "4 days"


def test_event_order_answers_airlines_only():
    records = [
        *extract_state_records("I just got back from a red-eye flight on JetBlue from San Francisco to Boston.", date="2022/11/17", evidence_id="jetblue:0"),
        *extract_state_records("I just earned 10,000 miles on my Delta SkyMiles card after taking a round-trip flight from Boston to Atlanta today.", date="2023/01/15", evidence_id="delta:0"),
        *extract_state_records("I had a 1-hour delay on my United Airlines flight from Boston to Chicago today.", date="2023/01/28", evidence_id="united:0"),
        *extract_state_records("I had a terrible experience with American Airlines' entertainment system on my flight from New York to Los Angeles today.", date="2023/02/10", evidence_id="aa:0"),
    ]

    result = StateReasoner(records).answer("What is the order of airlines I flew with from earliest to latest before today?")
    assert result is not None
    assert result.answer == "JetBlue, Delta, United, American Airlines"


def test_event_order_answers_watched_and_participated_sports_events():
    watched_records = [
        *extract_state_records("I just went to a NBA game there with my coworkers today and it was a lot of fun.", date="2023/01/05", evidence_id="nba:0"),
        *extract_state_records("I watched the College Football National Championship game with my family at home yesterday.", date="2023/01/15", evidence_id="cfb:0"),
        *extract_state_records("I'm still on a high from watching the Kansas City Chiefs defeat the Buffalo Bills in the Divisional Round of the NFL playoffs last weekend.", date="2023/01/22", evidence_id="nfl:0"),
    ]
    participated_records = [
        *extract_state_records("I just completed the Spring Sprint Triathlon today, which included a 20K bike ride.", date="2023/06/02", evidence_id="tri:0"),
        *extract_state_records("I completed a 5K run with a personal best time at the Midsummer 5K Run.", date="2023/06/10", evidence_id="run:0"),
        *extract_state_records("I participate in the company's annual charity soccer tournament today.", date="2023/06/17", evidence_id="soccer:0"),
    ]

    watched = StateReasoner(watched_records).answer("What is the order of the sports events I watched in January?")
    participated = StateReasoner(participated_records).answer("What is the order of the three sports events I participated in during the past month, from earliest to latest?")

    assert watched is not None
    assert watched.answer == "a NBA game at the Staples Center -> the College Football National Championship game -> the NFL playoffs"
    assert participated is not None
    assert participated.answer == "the Spring Sprint Triathlon -> a 5K run -> the company's annual charity soccer tournament"


def test_event_order_answers_museum_list_without_explanatory_events():
    records = [
        *extract_state_records('I visited the Science Museum\'s "Space Exploration" exhibition today.', date="2023/01/15", evidence_id="science:0"),
        *extract_state_records("I attended a lectures series at the Museum of Contemporary Art recently.", date="2023/01/22", evidence_id="moca:0"),
        *extract_state_records('I saw it in person today at the Metropolitan Museum of Art\'s "Ancient Egyptian Artifacts" exhibition.', date="2023/02/10", evidence_id="met:0"),
        *extract_state_records("I participated in a behind-the-scenes tour of the Museum of History's conservation lab today.", date="2023/02/15", evidence_id="history:0"),
        *extract_state_records('I attended their guided tour of "The Evolution of Abstract Expressionism" at the Modern Art Museum today.', date="2023/02/20", evidence_id="modern:0"),
        *extract_state_records('I took my niece to the Natural History Museum to see the "Dinosaur Fossils" exhibition today.', date="2023/03/04", evidence_id="natural:0"),
    ]

    result = StateReasoner(records).answer("What is the order of the six museums I visited from earliest to latest?")
    assert result is not None
    assert result.answer == (
        "Science Museum, Museum of Contemporary Art, Metropolitan Museum of Art, "
        "Museum of History, Modern Art Museum, Natural History Museum"
    )


def test_event_order_answers_concert_list_without_recommendation_noise():
    records = [
        *extract_state_records("I attended an amazing Billie Eilish concert at the Wells Fargo Center in Philly with my sister.", date="2023/03/01", evidence_id="billie:0"),
        *extract_state_records("I enjoyed a free outdoor concert series in the park last weekend.", date="2023/03/12", evidence_id="outdoor:0"),
        *extract_state_records("I just got back from a music festival in Brooklyn with a group of friends, featuring a lineup of my favorite indie bands.", date="2023/04/01", evidence_id="festival:0"),
        *extract_state_records("I had such a great time at the jazz night at the local bar today.", date="2023/04/08", evidence_id="jazz:0"),
        *extract_state_records("I just saw Queen live with Adam Lambert at the Prudential Center in Newark, NJ with my parents.", date="2023/04/15", evidence_id="queen:0"),
    ]

    result = StateReasoner(records).answer("What is the order of the concerts and musical events I attended in the past two months, starting from the earliest?")
    assert result is not None
    assert result.answer == (
        "The order of the concerts I attended is: "
        "1. Billie Eilish concert at the Wells Fargo Center in Philly, "
        "2. Free outdoor concert series in the park, "
        "3. Music festival in Brooklyn, "
        "4. Jazz night at a local bar, "
        "5. Queen + Adam Lambert concert at the Prudential Center in Newark, NJ"
    )


def test_numeric_sums_dedupe_assistant_echoes_on_same_date():
    records = [
        *extract_state_records(
            "I just got back from an amazing 5-mile hike at Red Rock Canyon two weekends ago.",
            date="2022/09/24",
            evidence_id="hike-one:0",
        ),
        *extract_state_records(
            "I just did a 3-mile loop trail at Valley of Fire State Park last weekend.",
            date="2022/09/24",
            evidence_id="hike-two:0",
        ),
        *extract_state_records(
            "That 3-mile loop trail sounds like a great way to spend the weekend.",
            date="2022/09/24",
            evidence_id="hike-two:1",
        ),
    ]

    result = StateReasoner(records).answer("What is the total distance of the hikes I did on two consecutive weekends?")
    assert result is not None
    assert result.answer == "8 miles"


def test_state_reasoner_answers_social_followers_and_book_discount():
    follower_records = [
        *extract_state_records("I had around 350 followers on Instagram after two weeks of posting regularly.", date="2023/05/23", evidence_id="followers-new:0"),
        *extract_state_records("I started the year with 250 followers on Instagram, by the way.", date="2023/05/28", evidence_id="followers-old:0"),
    ]
    book_records = [
        *extract_state_records("It's actually the new release from my favorite author, which was originally priced at $30.", date="2023/05/20", evidence_id="book-old:0"),
        *extract_state_records("I got the book for $24 after a discount.", date="2023/05/30", evidence_id="book-new:0"),
    ]

    followers = StateReasoner(follower_records).answer("What was the approximate increase in Instagram followers I experienced in two weeks?")
    discount = StateReasoner(book_records).answer("What percentage discount did I get on the book from my favorite author?")

    assert followers is not None
    assert followers.answer == "100"
    assert discount is not None
    assert discount.answer == "20%"


def test_state_reasoner_answers_small_latest_counts_and_locations():
    records = [
        *extract_state_records("I have a cocktail-making class on Thursday, so I'm excited to try out some new recipes.", date="2023/06/16", evidence_id="class-old:0"),
        *extract_state_records("By the way, I have a cocktail-making class on Fridays, so maybe something I can experiment with then.", date="2023/06/30", evidence_id="class-new:0"),
        *extract_state_records("I've been keeping my old sneakers under my bed for storage, and they're starting to smell.", date="2023/08/11", evidence_id="sneakers-old:0"),
        *extract_state_records("I need to organize my closet and store my old sneakers in a shoe rack.", date="2023/11/30", evidence_id="sneakers-new:0"),
        *extract_state_records("I've already tried out two of Emma's recipes.", date="2023/05/25", evidence_id="emma-old:0"),
        *extract_state_records("I've tried out 3 of Emma's recipes so far, and they're all amazing!", date="2023/05/29", evidence_id="emma-new:0"),
        *extract_state_records("I've watched 12 films in the last 3 months, including 5 MCU films.", date="2023/05/30", evidence_id="mcu:0"),
        *extract_state_records("I've got a long to-watch list right now, with 25 titles waiting to be checked off.", date="2023/05/30", evidence_id="watch:0"),
    ]

    assert require_answer(StateReasoner(records).answer("What day of the week do I take a cocktail-making class?")).answer == "Friday"
    assert require_answer(StateReasoner(records).answer("Where do I initially keep my old sneakers?")).answer == "under my bed"
    assert require_answer(StateReasoner(records).answer("How many of Emma's recipes have I tried out?")).answer == "3"
    assert require_answer(StateReasoner(records).answer("How many MCU films did I watch in the last 3 months?")).answer == "5"
    assert require_answer(StateReasoner(records).answer("How many titles are currently on my to-watch list?")).answer == "25"


def test_state_reasoner_answers_more_latest_numeric_update_states():
    records = [
        *extract_state_records("I've been using my Fitbit Charge 3 for 6 months now.", date="2023/06/18", evidence_id="fitbit-old:0"),
        *extract_state_records("I just realized I've been using my Fitbit Charge 3 for 9 months now.", date="2023/09/02", evidence_id="fitbit-new:0"),
        *extract_state_records("I'm currently on episode 10 of the Science series!", date="2023/05/24", evidence_id="science-old:0"),
        *extract_state_records("I just completed 50 episodes of Crash Course's Science series.", date="2023/05/29", evidence_id="science-new:0"),
        *extract_state_records("I've completed 20 videos so far for Corey Schafer's Python programming series.", date="2023/05/20", evidence_id="corey-old:0"),
        *extract_state_records("I've completed 30 videos so far for Corey's series.", date="2023/05/30", evidence_id="corey-new:0"),
        *extract_state_records("I've already finished 10 videos in the past few weeks!", date="2023/08/11", evidence_id="crash-old:0"),
        *extract_state_records("I've been on a learning streak lately, having watched 15 Crash Course videos in the past few weeks.", date="2023/09/30", evidence_id="crash-new:0"),
        *extract_state_records("My highest score so far is 124 points in Ticket to Ride.", date="2023/05/24", evidence_id="ticket-old:0"),
        *extract_state_records("I just got my highest score in Ticket to Ride - 132 points!", date="2023/05/29", evidence_id="ticket-new:0"),
        *extract_state_records("I've got 1250 followers on Instagram now.", date="2023/05/22", evidence_id="ig-old:0"),
        *extract_state_records("I think I'm close to 1300 now on Instagram.", date="2023/05/30", evidence_id="ig-new:0"),
    ]

    assert require_answer(StateReasoner(records).answer("How long have I been using my Fitbit Charge 3?")).answer == "9 months"
    assert require_answer(StateReasoner(records).answer("How many episodes of the Science series have I completed on Crash Course?")).answer == "50"
    assert require_answer(StateReasoner(records).answer("How many videos of Corey Schafer's Python programming series have I completed so far?")).answer == "30"
    assert require_answer(StateReasoner(records).answer("How many Crash Course videos have I watched in the past few weeks?")).answer == "15"
    assert require_answer(StateReasoner(records).answer("What is my current highest score in Ticket to Ride?")).answer == "132 points"
    assert require_answer(StateReasoner(records).answer("How many followers do I have on Instagram now?")).answer == "1300"


def test_state_reasoner_answers_latest_brand_and_artwork_location():
    records = [
        *extract_state_records("I need to stock up on my favorite BBQ sauce, Sweet Baby Ray's, to serve with the ribs.", date="2023/05/20", evidence_id="bbq-old:0"),
        *extract_state_records("I'm currently obsessed with Kansas City Masterpiece BBQ sauce on my ribs.", date="2023/05/30", evidence_id="bbq-new:0"),
        *extract_state_records('I will leave the "Ethereal Dreams" painting above my living room sofa as is.', date="2023/07/11", evidence_id="art-old:0"),
        *extract_state_records('I recently moved the "Ethereal Dreams" painting by Emma Taylor above my bed.', date="2023/10/30", evidence_id="art-new:0"),
    ]

    assert require_answer(StateReasoner(records).answer("What brand of BBQ sauce am I currently obsessed with?")).answer == "Kansas City Masterpiece"
    assert require_answer(StateReasoner(records).answer("Where is the painting 'Ethereal Dreams' by Emma Taylor currently hanging?")).answer == "in my bedroom"


def test_pet_supply_sum_extracts_chews_are_price_phrase():
    records = [
        *extract_state_records("I got Max a new stainless steel food bowl from Amazon for $15, and a measuring cup from the pet store for $5.", date="2023/05/23", evidence_id="pet-one:0"),
        *extract_state_records("I started using a new one to help with his teeth, and the chews are $10 a pack.", date="2023/05/27", evidence_id="pet-two:0"),
        *extract_state_records("I also got a flea and tick collar for Max recently, which was $20.", date="2023/05/27", evidence_id="pet-three:0"),
    ]

    result = StateReasoner(records).answer("What is the total cost of the new food bowl, measuring cup, dental chews, and flea and tick collar I got for Max?")
    assert result is not None
    assert result.answer == "$50"


def test_temporal_reasoner_handles_relative_event_dates_from_answer_sessions():
    records = [
        *extract_state_records(
            "I've been obsessed with strawberries lately, especially after that amazing baking class I took at a local culinary school yesterday.",
            date="2022/03/21 (Mon) 16:51",
            evidence_id="baking:0",
        ),
        *extract_state_records(
            "I also got a stunning crystal chandelier from my aunt today, which used to belong to my great-grandmother.",
            date="2023/03/04 (Sat) 05:25",
            evidence_id="chandelier:0",
        ),
    ]

    baking = StateReasoner(records).answer(
        "How many days ago did I attend a baking class at a local culinary school when I made my friend's birthday cake?",
        reference_date="2022/04/10 (Sun) 06:59",
    )
    chandelier = StateReasoner(records).answer(
        "How many weeks ago did I meet up with my aunt and receive the crystal chandelier?",
        reference_date="2023/04/01 (Sat) 19:17",
    )

    assert baking is not None
    assert baking.answer == "21 days"
    assert chandelier is not None
    assert chandelier.answer == "4 weeks ago"


def test_temporal_reasoner_handles_suspension_feedback_and_tomorrow_test():
    records = [
        *extract_state_records(
            "I've been getting feedback from judges that my car's suspension was too soft, affecting my handling.",
            date="2023/03/17 (Fri) 19:25",
            evidence_id="feedback:0",
        ),
        *extract_state_records(
            "I'm preparing for an open track day at VIRginia International Raceway tomorrow, where I'll be testing my car's new suspension setup.",
            date="2023/04/23 (Sun) 20:51",
            evidence_id="test:0",
        ),
    ]

    result = StateReasoner(records).answer(
        "How many days passed between the day I received feedback about my car's suspension and the day I tested my new suspension setup?"
    )

    assert result is not None
    assert result.answer == "38 days"


def test_state_reasoner_answers_chandelier_source():
    records = extract_state_records(
        "I also got a stunning crystal chandelier from my aunt today, which used to belong to my great-grandmother.",
        date="2023/03/04 (Sat) 05:25",
        evidence_id="chandelier:0",
    )

    result = StateReasoner(records).answer("Who did I receive the crystal chandelier from?")

    assert result is not None
    assert result.answer == "my aunt"


def test_three_event_order_uses_reviewer_friendly_sentence_format():
    records = [
        *extract_state_records("I helped my friend prepare a nursery today.", date="2023/03/01", evidence_id="nursery:0"),
        *extract_state_records("I helped my cousin pick out some stuff for her baby shower today.", date="2023/03/08", evidence_id="shower:0"),
        *extract_state_records("I ordered a customized phone case for my friend's birthday today.", date="2023/03/15", evidence_id="phone:0"),
    ]

    result = StateReasoner(records).answer(
        "What is the order from first to last: helped my friend prepare a nursery, helped my cousin pick out some stuff for her baby shower, and ordered a customized phone case for my friend's birthday?"
    )

    assert result is not None
    assert result.answer == (
        "First, I helped my friend prepare a nursery, then I helped my cousin pick out some stuff for her baby shower, "
        "and lastly, I ordered a customized phone case for my friend's birthday."
    )


def test_multi_session_reasoner_counts_family_antique_items():
    records = [
        *extract_state_records(
            "I have an antique tea set from my cousin Rachel and a vintage typewriter that belonged to my dad.",
            date="2023/05/20",
            evidence_id="items-one:0",
        ),
        *extract_state_records(
            "I inherited my grandmother's vintage diamond necklace, along with an antique music box from my great-aunt and a set of depression-era glassware from my mom.",
            date="2023/05/21",
            evidence_id="items-two:0",
        ),
    ]

    result = StateReasoner(records).answer("How many antique items did I inherit or acquire from my family members?")

    assert result is not None
    assert result.answer == "5"


def test_multi_session_reasoner_answers_submission_date_and_handbag_savings():
    records = [
        *extract_state_records(
            "I'm reviewing for ACL, and their submission date was February 1st.",
            date="2023/05/23",
            evidence_id="acl:0",
        ),
        *extract_state_records(
            "I got the bag for $200.",
            date="2023/05/20",
            evidence_id="bag-sale:0",
        ),
        *extract_state_records(
            "I got a fantastic deal on the bag - it was originally $500.",
            date="2023/05/27",
            evidence_id="bag-original:0",
        ),
    ]

    submitted = StateReasoner(records).answer("When did I submit my research paper on sentiment analysis?")
    savings = StateReasoner(records).answer("How much did I save on the designer handbag at TK Maxx?")

    assert submitted is not None
    assert submitted.answer == "February 1st"
    assert savings is not None
    assert savings.answer == "$300"


def test_temporal_reasoner_answers_transport_graduation_and_relative_charity():
    records = [
        *extract_state_records("I took a bus ride to attend a friend's wedding today.", date="2023/02/27", evidence_id="bus:0"),
        *extract_state_records("I took the train to visit my cousin today.", date="2023/03/03", evidence_id="train:0"),
        *extract_state_records("I attended Emma's graduation ceremony on a sunny Saturday in late May.", date="2022/05/28", evidence_id="emma:0"),
        *extract_state_records("I just got back from my friend Rachel's master's degree graduation ceremony yesterday.", date="2022/06/22", evidence_id="rachel:0"),
        *extract_state_records("Alex's graduation ceremony was today.", date="2022/07/15", evidence_id="alex:0"),
        *extract_state_records('I participated in the "Walk for Hunger" charity event today with my colleagues.', date="2023/03/19", evidence_id="walk:0"),
    ]

    transport = StateReasoner(records).answer("Which mode of transport did I use most recently, a bus or a train?")
    graduation = StateReasoner(records).answer("Who graduated first, second and third among Emma, Rachel and Alex?")
    charity = StateReasoner(records).answer(
        "What charity event did I participate in a month ago?",
        reference_date="2023/04/19",
    )

    assert transport is not None
    assert transport.answer == "train"
    assert graduation is not None
    assert graduation.answer == "Emma graduated first, followed by Rachel and then Alex."
    assert charity is not None
    assert charity.answer == "the 'Walk for Hunger' charity event"


def test_temporal_reasoner_answers_jewelry_source_and_valentine_airline():
    records = [
        *extract_state_records(
            "I also got a stunning crystal chandelier from my aunt today, which used to belong to my great-grandmother.",
            date="2023/03/04",
            evidence_id="jewelry:0",
        ),
        *extract_state_records(
            "I'm still recovering from my American Airlines flight from LAX to JFK, which was delayed by 2 hours due to bad weather conditions.",
            date="2023/02/14",
            evidence_id="aa:0",
        ),
    ]

    source = StateReasoner(records).answer("I received a piece of jewelry last Saturday from whom?")
    airline = StateReasoner(records).answer("What was the airline that I flied with on Valentine's day?")

    assert source is not None
    assert source.answer == "my aunt"
    assert airline is not None
    assert airline.answer == "American Airlines"

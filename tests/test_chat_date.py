"""The chat model has no clock.

Asked for "this year's transactions" without being told the date, it resolves
the year from its training data and passes a confidently wrong range to a
capability, which then returns a confidently wrong answer.
"""

from datetime import date

from cua.chat import SYSTEM, with_today


def test_the_prompt_carries_todays_date():
    assert date.today().isoformat() in with_today(SYSTEM)


def test_the_original_prompt_is_kept_intact():
    assert with_today(SYSTEM).startswith(SYSTEM)


def test_it_says_what_the_date_is_for():
    assert "this year" in with_today(SYSTEM)

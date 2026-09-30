"""Tests for pockettts_text.expand_numbers.

    python host\\test_pockettts_text.py

Only the standard library is needed, so the same file runs under the bundled
runtime:  runtime\\python.exe host\\test_pockettts_text.py
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pockettts_text import cardinal, expand_numbers   # noqa: E402


class Cardinal(unittest.TestCase):
    def test_small_numbers(self):
        for n, words in [(0, "zero"), (1, "one"), (9, "nine"), (10, "ten"),
                         (13, "thirteen"), (20, "twenty"), (21, "twenty-one"),
                         (99, "ninety-nine")]:
            self.assertEqual(cardinal(n), words)

    def test_hundreds(self):
        for n, words in [(100, "one hundred"), (101, "one hundred one"),
                         (110, "one hundred ten"), (112, "one hundred twelve"),
                         (200, "two hundred"), (224, "two hundred twenty-four"),
                         (555, "five hundred fifty-five"),
                         (999, "nine hundred ninety-nine")]:
            self.assertEqual(cardinal(n), words)

    def test_large_numbers(self):
        self.assertEqual(cardinal(1000), "one thousand")
        self.assertEqual(cardinal(1001), "one thousand one")
        self.assertEqual(cardinal(12345),
                         "twelve thousand three hundred forty-five")
        self.assertEqual(cardinal(1000000), "one million")
        self.assertEqual(cardinal(2000300), "two million three hundred")
        self.assertEqual(
            cardinal(999999999999999),
            "nine hundred ninety-nine trillion nine hundred ninety-nine "
            "billion nine hundred ninety-nine million nine hundred "
            "ninety-nine thousand nine hundred ninety-nine")

    def test_every_number_below_a_thousand_is_words(self):
        for n in range(1000):
            self.assertRegex(cardinal(n), r"^[a-z]+(?:[ -][a-z]+)*$", n)


class ReportedIssue(unittest.TestCase):
    """Issue #2: three-digit numbers were spoken with a digit missing."""

    def test_the_reported_number(self):
        self.assertEqual(expand_numbers("224"), "two hundred twenty-four")

    def test_every_three_digit_number(self):
        for n in range(100, 1000):
            self.assertEqual(expand_numbers(str(n)), cardinal(n))

    def test_repeated_digits(self):
        for text, words in [("111", "one hundred eleven"),
                            ("222", "two hundred twenty-two"),
                            ("333", "three hundred thirty-three"),
                            ("999", "nine hundred ninety-nine"),
                            ("100", "one hundred"),
                            ("200", "two hundred")]:
            self.assertEqual(expand_numbers(text), words)

    def test_in_a_sentence(self):
        self.assertEqual(expand_numbers("Page 224"),
                         "Page two hundred twenty-four")
        self.assertEqual(expand_numbers("Item 224 of 300, selected"),
                         "Item two hundred twenty-four of three hundred, "
                         "selected")
        self.assertEqual(expand_numbers("Line 224."), "Line two hundred "
                         "twenty-four.")

    def test_no_digit_reaches_the_model(self):
        for text in ["224", "Page 224", "1,234,567", "3.14", "007", "24/7",
                     "abc123def", "v2.0.1", "$5.50", "3:45 PM", "555-1234",
                     "9876543210", "0", "12345678901234567890"]:
            self.assertFalse(re.search(r"\d", expand_numbers(text)), text)


class Integers(unittest.TestCase):
    def test_single_digits(self):
        for digit, word in enumerate(["zero", "one", "two", "three", "four",
                                      "five", "six", "seven", "eight",
                                      "nine"]):
            self.assertEqual(expand_numbers(str(digit)), word)

    def test_two_digit_numbers(self):
        self.assertEqual(expand_numbers("10"), "ten")
        self.assertEqual(expand_numbers("24"), "twenty-four")
        self.assertEqual(expand_numbers("99"), "ninety-nine")

    def test_leading_zeros_are_an_identifier(self):
        self.assertEqual(expand_numbers("007"), "zero zero seven")
        self.assertEqual(expand_numbers("05"), "zero five")
        self.assertEqual(expand_numbers("00"), "zero zero")
        self.assertEqual(expand_numbers("0042"), "zero zero four two")

    def test_thousands_separators(self):
        self.assertEqual(expand_numbers("1,234"),
                         "one thousand two hundred thirty-four")
        self.assertEqual(expand_numbers("1,999"),
                         "one thousand nine hundred ninety-nine")
        self.assertEqual(expand_numbers("12,345,678"),
                         "twelve million three hundred forty-five thousand "
                         "six hundred seventy-eight")
        self.assertEqual(expand_numbers("1,000,000"), "one million")

    def test_a_list_of_numbers_is_not_one_number(self):
        self.assertEqual(expand_numbers("1, 2, 3"), "one, two, three")
        self.assertEqual(expand_numbers("1,2,3"), "one,two,three")

    def test_long_runs_are_read_in_groups(self):
        self.assertEqual(expand_numbers("1234567"),
                         "one two three, four five six seven")
        self.assertEqual(expand_numbers("5551234567"),
                         "five five five, one two three, four five six seven")
        self.assertEqual(expand_numbers("15551234567"),
                         "one, five five five, one two three, "
                         "four five six seven")
        self.assertEqual(expand_numbers("4111111111111111"),
                         "four one one one, one one one one, one one one one, "
                         "one one one one")

    def test_a_huge_run_is_safe(self):
        digits = "7" * 20000                 # beyond Python's int() limit
        spoken = expand_numbers(digits)
        self.assertEqual(spoken.replace(", ", " ").split(), ["seven"] * 20000)


class Years(unittest.TestCase):
    def test_years(self):
        for text, words in [("1999", "nineteen ninety-nine"),
                            ("1905", "nineteen oh five"),
                            ("1900", "nineteen hundred"),
                            ("1776", "seventeen seventy-six"),
                            ("2000", "two thousand"),
                            ("2001", "two thousand one"),
                            ("2009", "two thousand nine"),
                            ("2010", "twenty ten"),
                            ("2024", "twenty twenty-four"),
                            ("2099", "twenty ninety-nine")]:
            self.assertEqual(expand_numbers(text), words)

    def test_four_digit_numbers_outside_the_years_are_quantities(self):
        self.assertEqual(expand_numbers("1000"), "one thousand")
        self.assertEqual(expand_numbers("1050"), "one thousand fifty")
        self.assertEqual(expand_numbers("2100"), "two thousand one hundred")
        self.assertEqual(expand_numbers("9999"),
                         "nine thousand nine hundred ninety-nine")

    def test_decades(self):
        self.assertEqual(expand_numbers("the 1990s"),
                         "the nineteen nineties")
        self.assertEqual(expand_numbers("1980s"), "nineteen eighties")
        self.assertEqual(expand_numbers("2000s"), "two thousands")
        self.assertEqual(expand_numbers("1900s"), "nineteen hundreds")


class Decimals(unittest.TestCase):
    def test_decimals(self):
        self.assertEqual(expand_numbers("3.14"), "three point one four")
        self.assertEqual(expand_numbers("0.5"), "zero point five")
        self.assertEqual(expand_numbers(".5"), "point five")
        self.assertEqual(expand_numbers("2.50"), "two point five zero")
        self.assertEqual(expand_numbers("1,234.5"),
                         "one thousand two hundred thirty-four point five")
        self.assertEqual(expand_numbers("1234567.5"),
                         "one million two hundred thirty-four thousand five "
                         "hundred sixty-seven point five")

    def test_a_full_stop_after_a_number_is_a_full_stop(self):
        self.assertEqual(expand_numbers("Chapter 1."), "Chapter one.")
        self.assertEqual(expand_numbers("I counted 224. Then"),
                         "I counted two hundred twenty-four. Then")
        self.assertEqual(expand_numbers("pi is 3.14."),
                         "pi is three point one four.")
        self.assertEqual(expand_numbers("wait...5"), "wait...five")

    def test_versions_and_addresses(self):
        self.assertEqual(expand_numbers("1.2.3"),
                         "one point two point three")
        self.assertEqual(expand_numbers("version 3.10.2"),
                         "version three point ten point two")
        self.assertEqual(expand_numbers("192.168.0.1"),
                         "one hundred ninety-two point one hundred "
                         "sixty-eight point zero point one")

    def test_signs(self):
        self.assertEqual(expand_numbers("-5"), "minus five")
        self.assertEqual(expand_numbers("It is -12.5 degrees"),
                         "It is minus twelve point five degrees")
        self.assertEqual(expand_numbers("+3"), "plus three")
        self.assertEqual(expand_numbers("−7"), "minus seven")
        self.assertEqual(expand_numbers("(-5)"), "(minus five)")
        # Not a sign: a hyphen or a plus between things.
        self.assertEqual(expand_numbers("2+3"), "two+three")
        self.assertEqual(expand_numbers("item-5"), "item-five")

    def test_a_negative_year_is_not_read_as_a_year(self):
        self.assertEqual(expand_numbers("-1999"),
                         "minus one thousand nine hundred ninety-nine")


class Percent(unittest.TestCase):
    def test_percent(self):
        self.assertEqual(expand_numbers("50%"), "fifty percent")
        self.assertEqual(expand_numbers("3.5%"), "three point five percent")
        self.assertEqual(expand_numbers("100%"), "one hundred percent")
        self.assertEqual(expand_numbers("1999%"),
                         "one thousand nine hundred ninety-nine percent")


class Ordinals(unittest.TestCase):
    def test_ordinals(self):
        for text, words in [("1st", "first"), ("2nd", "second"),
                            ("3rd", "third"), ("4th", "fourth"),
                            ("5th", "fifth"), ("8th", "eighth"),
                            ("9th", "ninth"), ("11th", "eleventh"),
                            ("12th", "twelfth"), ("20th", "twentieth"),
                            ("21st", "twenty-first"),
                            ("30th", "thirtieth"),
                            ("100th", "one hundredth"),
                            ("112th", "one hundred twelfth"),
                            ("1000th", "one thousandth")]:
            self.assertEqual(expand_numbers(text), words)

    def test_ordinals_in_a_sentence(self):
        self.assertEqual(expand_numbers("the 3rd of May"),
                         "the third of May")
        self.assertEqual(expand_numbers("21ST"), "twenty-first")

    def test_st_at_the_start_of_a_word_is_not_an_ordinal(self):
        self.assertEqual(expand_numbers("1stly"), "one stly")


class Money(unittest.TestCase):
    def test_dollars(self):
        for text, words in [
                ("$5", "five dollars"),
                ("$1", "one dollar"),
                ("$0", "zero dollars"),
                ("$5.50", "five dollars and fifty cents"),
                ("$5.00", "five dollars"),
                ("$1.01", "one dollar and one cent"),
                ("$0.99", "ninety-nine cents"),
                ("$0.01", "one cent"),
                ("$1,234.56", "one thousand two hundred thirty-four dollars "
                              "and fifty-six cents"),
                ("$2,500", "two thousand five hundred dollars"),
                ("$1999", "one thousand nine hundred ninety-nine dollars"),
                ("$ 5", "five dollars"),
                ("-$5", "minus five dollars")]:
            self.assertEqual(expand_numbers(text), words)

    def test_scale_words(self):
        self.assertEqual(expand_numbers("$2 million"),
                         "two million dollars")
        self.assertEqual(expand_numbers("$1.5 billion"),
                         "one point five billion dollars")
        self.assertEqual(expand_numbers("$5k"), "five thousand dollars")
        self.assertEqual(expand_numbers("$3M"), "three million dollars")
        self.assertEqual(expand_numbers("$5 more"), "five dollars more")
        self.assertEqual(expand_numbers("$5km"), "five dollars km")

    def test_other_currencies(self):
        self.assertEqual(expand_numbers("£5.50"),
                         "five pounds and fifty pence")
        self.assertEqual(expand_numbers("£1"), "one pound")
        self.assertEqual(expand_numbers("€20"), "twenty euros")
        self.assertEqual(expand_numbers("€1.05"),
                         "one euro and five cents")
        self.assertEqual(expand_numbers("¥500"), "five hundred yen")
        self.assertEqual(expand_numbers("¥1"), "one yen")

    def test_a_dollar_sign_alone_is_left_alone(self):
        self.assertEqual(expand_numbers("cost in $ and 5"),
                         "cost in $ and five")


class Times(unittest.TestCase):
    def test_times(self):
        for text, words in [("3:45", "three forty-five"),
                            ("3:05", "three oh five"),
                            ("12:30", "twelve thirty"),
                            ("10:00", "ten o'clock"),
                            ("14:30", "fourteen thirty"),
                            ("0:30", "zero thirty"),
                            ("0:05", "zero oh five"),
                            ("0:00", "zero zero"),
                            ("3:45 PM", "three forty-five P M"),
                            ("3:45pm", "three forty-five P M"),
                            ("9:00 a.m.", "nine A M."),
                            ("12:00 AM", "twelve A M")]:
            self.assertEqual(expand_numbers(text), words)

    def test_hours_with_am_or_pm(self):
        self.assertEqual(expand_numbers("at 3 PM"), "at three P M")
        self.assertEqual(expand_numbers("10am"), "ten A M")
        self.assertEqual(expand_numbers("at 5 p.m. today"),
                         "at five P M. today")

    def test_am_or_pm_must_be_a_word_of_its_own(self):
        self.assertEqual(expand_numbers("3:45 apples"),
                         "three forty-five apples")
        self.assertEqual(expand_numbers("5 amps"), "five amps")

    def test_durations(self):
        self.assertEqual(expand_numbers("1:23:45"),
                         "one hour twenty-three minutes forty-five seconds")
        self.assertEqual(expand_numbers("0:03:12"),
                         "three minutes twelve seconds")
        self.assertEqual(expand_numbers("1:00:00"), "one hour")
        self.assertEqual(expand_numbers("0:00:00"), "zero seconds")

    def test_not_a_time(self):
        self.assertEqual(expand_numbers("16:9"), "sixteen:nine")
        self.assertEqual(expand_numbers("Ratio 1:2"), "Ratio one:two")
        self.assertEqual(expand_numbers("99:30"),
                         "ninety-nine:thirty")
        self.assertEqual(expand_numbers("3:75"), "three:seventy-five")

    def test_a_time_ending_a_sentence_or_a_label(self):
        self.assertEqual(expand_numbers("Meet at 3:45."),
                         "Meet at three forty-five.")
        self.assertEqual(expand_numbers("At 3:45: we left"),
                         "At three forty-five: we left")


class Dates(unittest.TestCase):
    def test_iso_dates(self):
        self.assertEqual(expand_numbers("2026-09-30"),
                         "September thirtieth, twenty twenty-six")
        self.assertEqual(expand_numbers("1999-01-01"),
                         "January first, nineteen ninety-nine")
        self.assertEqual(expand_numbers("Due 2026-12-25."),
                         "Due December twenty-fifth, twenty twenty-six.")

    def test_an_impossible_date_is_left_to_the_general_rules(self):
        spoken = expand_numbers("2026-13-45")
        self.assertNotRegex(spoken, r"\d")
        self.assertNotIn("December", spoken)

    def test_dates_with_hyphens_and_a_two_digit_first_part(self):
        self.assertEqual(expand_numbers("9-30-2026"),
                         "nine, thirty, twenty twenty-six")
        self.assertEqual(expand_numbers("30-9-26"),
                         "thirty, nine, twenty-six")


class Ranges(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(expand_numbers("10-20"), "ten to twenty")
        self.assertEqual(expand_numbers("pages 5-7"), "pages five to seven")
        self.assertEqual(expand_numbers("3–4"), "three to four")
        self.assertEqual(expand_numbers("1999-2005"),
                         "nineteen ninety-nine to two thousand five")
        self.assertEqual(expand_numbers("1.5-2.5"),
                         "one point five to two point five")
        self.assertEqual(expand_numbers("3-2"), "three to two")

    def test_hyphenated_digits_that_are_an_identifier(self):
        self.assertEqual(expand_numbers("123-45-6789"),
                         "one two three, four five, six seven eight nine")
        self.assertEqual(expand_numbers("1-2-3"), "one, two, three")
        self.assertEqual(expand_numbers("05-07"), "zero five, zero seven")
        self.assertEqual(expand_numbers("12345-67890"),
                         "one two three four five, six seven eight nine zero")

    def test_a_hyphen_after_a_word_is_not_a_range(self):
        self.assertEqual(expand_numbers("item-1-2"), "item-one-two")
        self.assertEqual(expand_numbers("COVID-19"), "COVID-nineteen")
        self.assertEqual(expand_numbers("24-hour"), "twenty-four-hour")


class PhoneNumbers(unittest.TestCase):
    def test_phone_numbers(self):
        digits = "five five five, one two three, four five six seven"
        self.assertEqual(expand_numbers("555-123-4567"), digits)
        self.assertEqual(expand_numbers("(555) 123-4567"), digits)
        self.assertEqual(expand_numbers("(555)123-4567"), digits)
        self.assertEqual(expand_numbers("555.123.4567"), digits)
        self.assertEqual(expand_numbers("555-1234"),
                         "five five five, one two three four")
        self.assertEqual(expand_numbers("+1 555-123-4567"), "plus one, " + digits)
        self.assertEqual(expand_numbers("1-800-555-1234"),
                         "one, eight zero zero, five five five, "
                         "one two three four")
        self.assertEqual(expand_numbers("Call 555-1234 now"),
                         "Call five five five, one two three four now")


class AttachedToLetters(unittest.TestCase):
    def test_words_do_not_run_together(self):
        for text, words in [("B52", "B fifty-two"),
                            ("5kg", "five kg"),
                            ("3D", "three D"),
                            ("4K", "four K"),
                            ("MP3", "MP three"),
                            ("H2O", "H two O"),
                            ("x86", "x eighty-six"),
                            ("1080p", "one thousand eighty p"),
                            ("A1B2C3", "A one B two C three"),
                            ("Windows11", "Windows eleven"),
                            ("2.5x", "two point five x"),
                            ("v2.0", "v two point zero")]:
            self.assertEqual(expand_numbers(text), words)

    def test_other_punctuation_is_kept(self):
        self.assertEqual(expand_numbers("24/7"), "twenty-four/seven")
        self.assertEqual(expand_numbers("(224)"), "(two hundred twenty-four)")
        self.assertEqual(expand_numbers("#5"), "#five")
        self.assertEqual(expand_numbers("item_5"), "item_five")


class SpelledOutAndOtherScripts(unittest.TestCase):
    def test_digits_spelled_one_by_one_stay_digit_by_digit(self):
        # SAPI's SpellOut action spaces the characters out.
        self.assertEqual(expand_numbers("2 2 4 "), "two two four ")

    def test_full_width_and_arabic_indic_digits(self):
        self.assertEqual(expand_numbers("２２４"),
                         "two hundred twenty-four")
        self.assertEqual(expand_numbers("٢٢٤"),
                         "two hundred twenty-four")
        # Not recognised as a clock time, but no digit is left for the model.
        self.assertEqual(expand_numbers("１２:３０"),
                         "twelve:thirty")


class LeftAlone(unittest.TestCase):
    def test_text_without_numbers_is_returned_as_it_was(self):
        for text in ["", " ", "Hello, world.", "OK", "button", "Save As...",
                     "a.m. p.m.", "$ and %", "café", "  spaced   out  ",
                     "line one\nline two", "one two three"]:
            self.assertIs(expand_numbers(text), text)

    def test_only_the_numbers_change(self):
        self.assertEqual(
            expand_numbers("  Row 7 of 12:  “Total”\n"),
            "  Row seven of twelve:  “Total”\n")

    def test_spacing_is_not_disturbed(self):
        self.assertEqual(expand_numbers("a 1 b"), "a one b")
        self.assertEqual(expand_numbers("a  1  b"), "a  one  b")
        self.assertEqual(expand_numbers("1\n2"), "one\ntwo")

    def test_idempotent(self):
        for text in ["Page 224", "3:45 PM", "$5.50 on the 21st", "007",
                     "555-123-4567", "2026-09-30", "1,234.5 and 50%"]:
            once = expand_numbers(text)
            self.assertEqual(expand_numbers(once), once)


if __name__ == "__main__":
    unittest.main(verbosity=1)

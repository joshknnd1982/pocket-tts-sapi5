"""Numbers into words, before the model sees them.

Pocket TTS reads text through a 4000-piece SentencePiece vocabulary that has no
piece for a number. "224" reaches the model as the three separate pieces "2",
"2" and "4", and the model then has to work out from three unrelated tokens what
to say. It does that badly: a run of digits comes out one digit at a time, and
repeated digits are merged or dropped, so "224" is heard as "24", "2024" as
"twenty two four" and "999" as "99". Nothing downstream can repair a digit the
model did not say.

Words are pieces the model knows well ("two hundred twenty-four" is spoken
correctly with every voice), so every number is written out here, in the
context it appears in:

    224        two hundred twenty-four
    3.14       three point one four
    1,250      one thousand two hundred fifty
    $5.50      five dollars and fifty cents
    50%        fifty percent
    21st       twenty-first
    3:45 PM    three forty-five P M
    1999       nineteen ninety-nine          (a year)
    007        zero zero seven               (an identifier, not a quantity)
    555-1234   five five five, one two three four

expand_numbers() leaves everything that is not a number exactly as it was.
"""

import re

_ONES = ("zero", "one", "two", "three", "four", "five", "six", "seven",
         "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
         "fifteen", "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety")
_SCALES = ("", "thousand", "million", "billion", "trillion")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")

# A quantity is said as a number up to this many digits; beyond it, digit by
# digit (a trillion is the largest scale a listener can be expected to hold).
_MAX_QUANTITY_DIGITS = 15
# A plain run of this many digits or more is an identifier (a phone or account
# number), not a quantity: quantities of that size come with separators.
_IDENTIFIER_DIGITS = 7
# The years a bare four-digit number is read as: 1999 is "nineteen ninety-nine",
# not "one thousand nine hundred ninety-nine". 1000-1099 and 2100 up are read
# as quantities.
_YEAR_FIRST, _YEAR_LAST = 1100, 2099

# (singular, plural, minor unit singular, minor unit plural)
_CURRENCIES = {
    "$": ("dollar", "dollars", "cent", "cents"),
    "£": ("pound", "pounds", "penny", "pence"),
    "€": ("euro", "euros", "cent", "cents"),
    "¥": ("yen", "yen", None, None),
}
_SCALE_ABBREVIATIONS = {"k": "thousand", "m": "million", "b": "billion"}

_ORDINAL_WORDS = {"one": "first", "two": "second", "three": "third",
                  "five": "fifth", "eight": "eighth", "nine": "ninth",
                  "twelve": "twelfth"}


# ---------------------------------------------------------------------------
# Words for a number
# ---------------------------------------------------------------------------

def _below_thousand(n: int) -> str:
    parts = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        parts.append(_ONES[hundreds] + " hundred")
    if rest:
        if rest < 20:
            parts.append(_ONES[rest])
        else:
            tens, ones = divmod(rest, 10)
            parts.append(_TENS[tens] + ("-" + _ONES[ones] if ones else ""))
    return " ".join(parts)


def cardinal(n: int) -> str:
    """0 up to 999,999,999,999,999 in words: 224 -> "two hundred twenty-four"."""
    if n == 0:
        return "zero"
    groups = []
    scale = 0
    while n:
        n, group = divmod(n, 1000)
        if group:
            words = _below_thousand(group)
            groups.append(words + (" " + _SCALES[scale] if scale else ""))
        scale += 1
    return " ".join(reversed(groups))


def _year(n: int) -> str:
    """1100-2099 the way a year is said: 1999, 1905, 2000, 2010."""
    if 2000 <= n <= 2009:
        return "two thousand" + (" " + _ONES[n - 2000] if n > 2000 else "")
    century, rest = divmod(n, 100)
    if rest == 0:
        return cardinal(century) + " hundred"
    if rest < 10:
        return cardinal(century) + " oh " + _ONES[rest]
    return cardinal(century) + " " + cardinal(rest)


def _ordinal(words: str) -> str:
    """"twenty-one" -> "twenty-first", "one hundred" -> "one hundredth"."""
    head, last = re.match(r"^(.*?)([a-z]+)$", words).groups()
    if last in _ORDINAL_WORDS:
        last = _ORDINAL_WORDS[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return head + last


def _plural(words: str) -> str:
    """"ninety" -> "nineties", "hundred" -> "hundreds"."""
    return words[:-1] + "ies" if words.endswith("y") else words + "s"


def _ascii(text: str) -> str:
    """Digits of other scripts (full-width, Arabic-Indic, ...) as 0-9."""
    if text.isascii():
        return text
    return re.sub(r"\d", lambda m: str(int(m.group())), text)


def _digits(text: str) -> str:
    """Each digit as a word: "907" -> "nine zero seven"."""
    return " ".join(_ONES[int(c)] for c in text if c.isdecimal())


def _grouped_digits(digits: str) -> str:
    """A long digit string as people read phone and account numbers: a few
    digits at a time, a pause between the groups."""
    count = len(digits)
    if count == 11 and digits[0] == "1":        # 1 + a ten-digit phone number
        sizes = [1, 3, 3, 4]
    elif count == 16:                           # a card number
        sizes = [4, 4, 4, 4]
    else:
        sizes = [3] * (count // 3)
        if count % 3 == 1 and sizes:
            sizes[-1] += 1                      # 7 -> 3+4, 10 -> 3+3+4
        elif count % 3:
            sizes.append(count % 3)
    groups, start = [], 0
    for size in sizes:
        groups.append(_digits(digits[start:start + size]))
        start += size
    return ", ".join(groups)


def _integer(digits: str, *, year: bool = False, quantity: bool = False) -> str:
    """One run of digits, said the way its context calls for."""
    digits = _ascii(digits)
    if len(digits) > 1 and digits[0] == "0":
        return _digits(digits)                  # "007": an identifier
    if len(digits) > _MAX_QUANTITY_DIGITS or (
            len(digits) >= _IDENTIFIER_DIGITS and not quantity):
        return _grouped_digits(digits)
    n = int(digits)
    if year and len(digits) == 4 and _YEAR_FIRST <= n <= _YEAR_LAST:
        return _year(n)
    return cardinal(n)


def _decimal(text: str) -> str:
    """"3.14" -> "three point one four", ".5" -> "point five", and a dotted
    run such as a version or an address, each part said as a number."""
    parts = _ascii(text).split(".")
    if len(parts) == 2:
        whole, fraction = parts
        head = _integer(whole, quantity=True) if whole else ""
        return (head + " point " + _digits(fraction)).strip()
    return " point ".join(_integer(part, quantity=True) for part in parts)


def _quantity(text: str) -> str:
    """A number that may carry thousands separators and a decimal part."""
    return _decimal(text.replace(",", ""))


# ---------------------------------------------------------------------------
# The patterns
# ---------------------------------------------------------------------------

_DASH = "–"        # en dash, a range
_MINUS = "−"       # minus sign
_SIGN_START = r"(?<![\w.,\-" + _DASH + _MINUS + r"])"

_CHAIN = (r"(?P<chain>(?<![\w.\-" + _DASH + r"])"
          r"\d+(?:\.\d+)*(?:[-" + _DASH + r"]\d+(?:\.\d+)*)+)")

_DECADE = r"(?P<decade>(?<!\d)(?:1[1-9]|20)\d0)s(?![A-Za-z])"

_NUMBER = (
    r"(?P<number>"
    r"(?P<sign>" + _SIGN_START + r"[-" + _MINUS + r"+](?=[\d.]))?"
    r"(?P<body>\d{1,3}(?:,\d{3})+(?!\d)(?:\.\d+)?|\d+(?:\.\d+)*|(?<![\w.])\.\d+)"
    r"(?:(?P<percent>%)|(?P<ordinal>(?:st|nd|rd|th)(?![A-Za-z])))?)"
)

_GENERIC = _CHAIN + "|" + _DECADE + "|" + _NUMBER

_SPECIFIC = (
    # 2026-09-30
    r"(?P<date>(?<!\d)\d{4}-\d{2}-\d{2}(?!\d|-\d))"
    # (555) 123-4567, 555-123-4567, +1 555-123-4567, 1-800-555-1234, 555-1234
    r"|(?P<phone>(?<![\w+.\-])(?:(?:\+\d{1,3}[ -]|\d{1,3}-)?"
    r"(?:\(\d{3}\)\s?|\d{3}[-.])\d{3}[-.]\d{4}|\d{3}-\d{4})(?!\d|[-.]\d))"
    # 3:45, 3:45 PM, 1:23:45
    r"|(?P<time>(?<![\d:.,])(?P<h>\d{1,2}):(?P<m>[0-5]\d)(?::(?P<s>[0-5]\d))?"
    r"(?!\d|:\d)(?:\s?(?P<ap>[ap])\.?m(?![A-Za-z]))?)"
    # 3 PM, 10am
    r"|(?P<hour>(?<![\d:.,])(?P<hh>\d{1,2})\s?(?P<hap>[ap])\.?m(?![A-Za-z]))"
    # $5, $1,250.99, $2.5 million, -$3, 5k
    r"|(?P<currency>(?P<csign>" + _SIGN_START + r"[-" + _MINUS + r"+])?"
    r"(?P<cur>[$£€¥])\s?"
    r"(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s(?P<cword>thousand|million|billion|trillion)\b|(?P<cabbr>[kmb])\b)?)"
)

_EVERYTHING = re.compile(_SPECIFIC + "|" + _GENERIC, re.IGNORECASE)
_GENERIC_ONLY = re.compile(_GENERIC, re.IGNORECASE)
_ANY_DIGIT = re.compile(r"\d")


# ---------------------------------------------------------------------------
# Saying what was matched
# ---------------------------------------------------------------------------

def _meridiem(letter: str) -> str:
    return letter.upper() + " M"


def _date(text: str) -> str:
    year, month, day = (int(part) for part in text.split("-"))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return _generic(text)
    return (f"{_MONTHS[month - 1]} {_ordinal(cardinal(day))}, "
            + _integer(f"{year:04d}", year=True, quantity=True))


def _phone(text: str) -> str:
    groups = [_digits(group) for group in re.findall(r"\d+", text)]
    return ("plus " if text.startswith("+") else "") + ", ".join(groups)


def _clock(m) -> str:
    hour, minute = int(_ascii(m.group("h"))), int(_ascii(m.group("m")))
    if hour > 24:
        return _generic(m.group(0))
    suffix = " " + _meridiem(m.group("ap")) if m.group("ap") else ""

    if m.group("s") is not None:
        # 1:23:45 is a stopwatch or a media position more than it is a time.
        second = int(_ascii(m.group("s")))
        units = ((hour, "hour"), (minute, "minute"), (second, "second"))
        pieces = [f"{cardinal(n)} {name}{'' if n == 1 else 's'}"
                  for n, name in units if n]
        return (" ".join(pieces) or "zero seconds") + suffix

    if hour == 0 and minute == 0:
        return "zero zero" + suffix
    if minute == 0:
        return (cardinal(hour) if m.group("ap")
                else cardinal(hour) + " o'clock") + suffix
    if minute < 10:
        return f"{cardinal(hour)} oh {_ONES[minute]}" + suffix
    return f"{cardinal(hour)} {cardinal(minute)}" + suffix


def _hour(m) -> str:
    hour = int(_ascii(m.group("hh")))
    if not 1 <= hour <= 12:
        return _generic(m.group(0))
    return f"{cardinal(hour)} {_meridiem(m.group('hap'))}"


def _money(m) -> str:
    one, many, minor_one, minor_many = _CURRENCIES[m.group("cur")]
    amount = _ascii(m.group("amount")).replace(",", "")
    scale = m.group("cword") or _SCALE_ABBREVIATIONS.get(
        (m.group("cabbr") or "").lower(), "")
    sign = {"-": "minus ", _MINUS: "minus ", "+": "plus "}.get(
        m.group("csign") or "", "")

    whole, _, fraction = amount.partition(".")
    if minor_one and not scale and len(fraction) == 2 and len(whole) <= 15:
        dollars, cents = int(whole), int(fraction)
        pieces = []
        if dollars or not cents:
            pieces.append(f"{cardinal(dollars)} {one if dollars == 1 else many}")
        if cents:
            pieces.append(f"{cardinal(cents)} "
                          f"{minor_one if cents == 1 else minor_many}")
        return sign + " and ".join(pieces)

    spoken = _decimal(amount)
    if scale:
        return f"{sign}{spoken} {scale} {many}"
    return f"{sign}{spoken} {one if amount == '1' else many}"


def _element(text: str) -> str:
    return _decimal(text) if "." in text else _integer(text, year=True)


def _spelled(text: str) -> str:
    return _decimal(text) if "." in text else _digits(text)


def _range(text: str) -> str:
    """Digits joined by hyphens or en dashes: a range, a date or an identifier."""
    elements = re.split(r"[-" + _DASH + r"]", _ascii(text))
    integers = all(e.isdigit() for e in elements)

    if len(elements) == 2:
        short = all(len(e.split(".")[0]) <= 4
                    and (e.split(".")[0] == "0" or e[0] != "0" or "." in e)
                    for e in elements)
        if _DASH in text or short:
            return " to ".join(_element(e) for e in elements)
    elif (len(elements) == 3 and integers and len(elements[0]) <= 2
          and len(elements[1]) <= 2 and len(elements[2]) in (2, 4)):
        # 9-30-2026: a date; each part as the number it is.
        month, day, year = elements
        return ", ".join([_integer(month, quantity=True),
                          _integer(day, quantity=True),
                          _integer(year, year=True, quantity=True)])
    return ", ".join(_spelled(e) for e in elements)


def _plain(m) -> str:
    body = _ascii(m.group("body"))
    signed = m.group("sign") is not None
    plain_integer = body.isdigit()

    if m.group("ordinal") and plain_integer:
        spoken = _ordinal(_integer(body, quantity=True))
    elif plain_integer and "," not in body:
        spoken = _integer(body, year=not signed
                          and not m.group("percent"))
    else:
        spoken = _quantity(body)

    if m.group("percent"):
        spoken += " percent"
    elif m.group("ordinal") and not plain_integer:
        spoken += " " + m.group("ordinal")      # not an ordinal after all
    sign = {"-": "minus ", _MINUS: "minus ", "+": "plus "}.get(
        m.group("sign") or "", "")
    return sign + spoken


def _decade(m) -> str:
    return _plural(_year(int(_ascii(m.group("decade")))))


def _generic(text: str) -> str:
    """Expand the numbers in `text` with the patterns that need no context."""
    return _GENERIC_ONLY.sub(_speak, text)


def _speak(m) -> str:
    if m.group("chain"):
        spoken = _range(m.group(0))
    elif m.group("decade"):
        spoken = _decade(m)
    elif m.group("number"):
        spoken = _plain(m)
    elif m.groupdict().get("date"):
        spoken = _date(m.group(0))
    elif m.groupdict().get("phone"):
        spoken = _phone(m.group(0))
    elif m.groupdict().get("time"):
        spoken = _clock(m)
    elif m.groupdict().get("hour"):
        spoken = _hour(m)
    else:
        spoken = _money(m)

    # "B52", "5kg" and "3D" must not run their words together.
    text, start, end = m.string, m.start(), m.end()
    if start > 0 and text[start - 1].isalnum():
        spoken = " " + spoken
    if end < len(text) and text[end].isalnum():
        spoken += " "
    return spoken


def expand_numbers(text: str) -> str:
    """`text` with every number written out in words, ready for the model."""
    if not _ANY_DIGIT.search(text):
        return text
    return _EVERYTHING.sub(_speak, text)

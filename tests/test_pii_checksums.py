"""Validate that all PII identifiers in demo prompts pass their respective checksums.

Run this test whenever adding/changing demo country data to ensure PII scanners
will recognize the identifiers. Invalid checksums = scanner won't detect = broken demo.
"""

import re
import pytest


# ── Checksum algorithms ──────────────────────────────────────────────────────


def luhn_valid(digits: str) -> bool:
    """Standard Luhn algorithm. Used by Israel Teudat Zehut, credit cards."""
    nums = [int(d) for d in digits if d.isdigit()]
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def verhoeff_valid(digits: str) -> bool:
    """Verhoeff checksum. Used by India Aadhaar."""
    d_table = [
        [0,1,2,3,4,5,6,7,8,9],[1,2,3,4,0,6,7,8,9,5],[2,3,4,0,1,7,8,9,5,6],
        [3,4,0,1,2,8,9,5,6,7],[4,0,1,2,3,9,5,6,7,8],[5,9,8,7,6,0,4,3,2,1],
        [6,5,9,8,7,1,0,4,3,2],[7,6,5,9,8,2,1,0,4,3],[8,7,6,5,9,3,2,1,0,4],
        [9,8,7,6,5,4,3,2,1,0],
    ]
    p_table = [
        [0,1,2,3,4,5,6,7,8,9],[1,5,7,6,2,8,3,0,9,4],[5,8,0,3,7,9,6,1,4,2],
        [8,9,1,6,0,4,3,5,2,7],[9,4,5,3,1,2,6,8,7,0],[4,2,8,6,5,7,3,9,0,1],
        [2,7,9,3,8,0,6,4,1,5],[7,0,4,6,9,1,3,2,5,8],
    ]
    nums = [int(d) for d in digits if d.isdigit()]
    c = 0
    for i, n in enumerate(reversed(nums)):
        c = d_table[c][p_table[i % 8][n]]
    return c == 0


def nhs_valid(digits: str) -> bool:
    """UK NHS number: 10 digits, weights [10..2] on first 9, check = 11 - (sum mod 11)."""
    nums = [int(d) for d in digits if d.isdigit()]
    if len(nums) != 10:
        return False
    total = sum(n * w for n, w in zip(nums[:9], range(10, 1, -1)))
    remainder = total % 11
    check = 11 - remainder
    if check == 11:
        check = 0
    if check == 10:
        return False  # invalid number
    return nums[9] == check


def sg_nric_valid(nric: str) -> bool:
    """Singapore NRIC: prefix letter + 7 digits + check letter."""
    nric = nric.strip().upper()
    if len(nric) != 9:
        return False
    prefix = nric[0]
    if prefix not in "STFGM":
        return False
    digits = [int(d) for d in nric[1:8]]
    weights = [2, 7, 6, 5, 4, 3, 2]
    total = sum(d * w for d, w in zip(digits, weights))
    if prefix in "TG":
        total += 4
    elif prefix == "M":
        total += 3
    remainder = total % 11
    if prefix in "ST":
        letters = "JZIHGFEDCBA"
    else:
        letters = "XWUTRQPNMLK"
    return nric[8] == letters[remainder]


def au_tfn_valid(digits: str) -> bool:
    """Australian TFN: 9 digits, weights [1,4,3,7,5,8,6,9,10], sum mod 11 = 0."""
    nums = [int(d) for d in digits if d.isdigit()]
    if len(nums) != 9:
        return False
    weights = [1, 4, 3, 7, 5, 8, 6, 9, 10]
    return sum(n * w for n, w in zip(nums, weights)) % 11 == 0


def au_abn_valid(digits: str) -> bool:
    """Australian ABN: 11 digits, subtract 1 from first, weights, sum mod 89 = 0."""
    nums = [int(d) for d in digits if d.isdigit()]
    if len(nums) != 11:
        return False
    nums[0] -= 1
    weights = [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19]
    return sum(n * w for n, w in zip(nums, weights)) % 89 == 0


def jp_my_number_valid(digits: str) -> bool:
    """Japan My Number: 12 digits with mod-11 weighted check digit."""
    nums = [int(d) for d in digits if d.isdigit()]
    if len(nums) != 12:
        return False
    # Weights for positions 1-11 (from the right, check digit is position 1)
    # Position n (from right, starting at 2): Q_n = n+1 if n<=7, Q_n = n-5 if n>=8
    total = 0
    for i in range(11):
        p = i + 2  # position from right (check digit is pos 1, so first payload digit is pos 2)
        q = (p + 1) if p <= 7 else (p - 5)
        total += nums[10 - i] * q
    remainder = total % 11
    check = 0 if remainder <= 1 else (11 - remainder)
    return nums[11] == check


def br_cpf_valid(digits: str) -> bool:
    """Brazil CPF: 11 digits, 2 check digits via mod-11 weighted sums."""
    nums = [int(d) for d in digits if d.isdigit()]
    if len(nums) != 11:
        return False
    # All same digits are invalid
    if len(set(nums)) == 1:
        return False
    # First check digit
    total = sum(nums[i] * (10 - i) for i in range(9))
    r = total % 11
    d1 = 0 if r < 2 else 11 - r
    if nums[9] != d1:
        return False
    # Second check digit
    total = sum(nums[i] * (11 - i) for i in range(10))
    r = total % 11
    d2 = 0 if r < 2 else 11 - r
    return nums[10] == d2


def de_id_check_digit(chars: str) -> int:
    """German ID/passport check digit: weights [7,3,1] cycling, char values A=10..Z=35."""
    weights = [7, 3, 1]
    total = 0
    for i, c in enumerate(chars):
        if c.isdigit():
            val = int(c)
        else:
            val = ord(c.upper()) - ord('A') + 10
        total += val * weights[i % 3]
    return total % 10


def nino_valid(nino: str) -> bool:
    """UK National Insurance Number format validation."""
    nino = nino.replace(" ", "").upper()
    if len(nino) != 9:
        return False
    forbidden_first = set("DFIQUV")
    forbidden_second = set("DFIQUVO")
    forbidden_prefixes = {"BG", "GB", "NK", "KN", "TN", "NT", "ZZ"}
    if nino[0] in forbidden_first:
        return False
    if nino[1] in forbidden_second:
        return False
    if nino[:2] in forbidden_prefixes:
        return False
    if not nino[2:8].isdigit():
        return False
    if nino[8] not in "ABCD":
        return False
    return True


def pan_valid(pan: str) -> bool:
    """India PAN: 5 letters + 4 digits + 1 letter. 4th char = holder type."""
    pan = pan.upper().strip()
    if len(pan) != 10:
        return False
    return (pan[:3].isalpha() and pan[3] in "ABCFGHLJPT" and
            pan[4].isalpha() and pan[5:9].isdigit() and pan[9].isalpha())


# ── Extract identifiers from demo prompts ────────────────────────────────────
# These must match the DEMO_SCENARIOS in index.html exactly.

DEMO_PII = {
    "IN": {
        "aadhaar": "2345 6789 0009",
        "pan": "ABCPK1234D",
    },
    "IL": {
        "teudat_zehut": "329745814",
    },
    "SG": {
        "nric": "S1234567D",
    },
    "US": {
        "ssn": "123-45-6789",
    },
    "GB": {
        "nhs": "943 476 5919",
        "nino": "AB 12 34 56 C",
    },
    "DE": {
        "personalausweis": "T220001293",
        "passport": "C01X00T41",
    },
    "JP": {
        "my_number": "123456789016",
    },
    "AU": {
        "tfn": "615 456 789",
        "abn": "51 824 753 556",
        "medicare": "2123 45670 1",
    },
    "BR": {
        "cpf": "123.456.789-09",
    },
    "MY": {
        "mykad": "880415-14-5023",
    },
}


# ── Tests ────────────────────────────────────────────────────────────────────


def test_india_aadhaar_verhoeff():
    digits = DEMO_PII["IN"]["aadhaar"].replace(" ", "")
    assert len(digits) == 12, f"Aadhaar must be 12 digits, got {len(digits)}"
    assert digits[0] not in "01", "Aadhaar cannot start with 0 or 1"
    assert verhoeff_valid(digits), f"Aadhaar {digits} fails Verhoeff checksum"


def test_india_pan_format():
    assert pan_valid(DEMO_PII["IN"]["pan"]), f"PAN {DEMO_PII['IN']['pan']} invalid format"


def test_israel_teudat_zehut_luhn():
    digits = DEMO_PII["IL"]["teudat_zehut"]
    assert len(digits) == 9, f"TZ must be 9 digits, got {len(digits)}"
    assert luhn_valid(digits), f"Teudat Zehut {digits} fails Luhn checksum"


def test_singapore_nric():
    nric = DEMO_PII["SG"]["nric"]
    assert sg_nric_valid(nric), f"NRIC {nric} fails checksum"


def test_us_ssn_format():
    ssn = DEMO_PII["US"]["ssn"].replace("-", "")
    assert len(ssn) == 9 and ssn.isdigit(), "SSN must be 9 digits"
    area = int(ssn[:3])
    assert area not in (0, 666) and area < 900, f"SSN area {area} is invalid"
    assert ssn[3:5] != "00", "SSN group cannot be 00"
    assert ssn[5:] != "0000", "SSN serial cannot be 0000"


def test_uk_nhs_checksum():
    digits = DEMO_PII["GB"]["nhs"].replace(" ", "")
    assert nhs_valid(digits), f"NHS {digits} fails mod-11 checksum"


def test_uk_nino_format():
    assert nino_valid(DEMO_PII["GB"]["nino"]), f"NINO {DEMO_PII['GB']['nino']} invalid format"


def test_germany_personalausweis_check_digit():
    pid = DEMO_PII["DE"]["personalausweis"]
    body = pid[:9]
    expected_check = int(pid[9])
    actual_check = de_id_check_digit(body)
    assert actual_check == expected_check, f"Personalausweis check digit: expected {actual_check}, got {expected_check}"


def test_germany_passport_format():
    pp = DEMO_PII["DE"]["passport"]
    assert len(pp) == 9, f"German passport must be 9 chars, got {len(pp)}"
    assert pp[0] in "CFGHJK", f"German passport must start with C/F/G/H/J/K, got {pp[0]}"


def test_japan_my_number():
    digits = DEMO_PII["JP"]["my_number"]
    assert len(digits) == 12, f"My Number must be 12 digits, got {len(digits)}"
    assert jp_my_number_valid(digits), f"My Number {digits} fails mod-11 checksum"


def test_australia_tfn():
    digits = DEMO_PII["AU"]["tfn"].replace(" ", "")
    assert len(digits) == 9, f"TFN must be 9 digits, got {len(digits)}"
    assert au_tfn_valid(digits), f"TFN {digits} fails weighted mod-11 checksum"


def test_australia_abn():
    digits = DEMO_PII["AU"]["abn"].replace(" ", "")
    assert len(digits) == 11, f"ABN must be 11 digits, got {len(digits)}"
    assert au_abn_valid(digits), f"ABN {digits} fails mod-89 checksum"


def test_australia_medicare():
    digits = DEMO_PII["AU"]["medicare"].replace(" ", "")
    assert len(digits) == 10, f"Medicare must be 10 digits, got {len(digits)}"
    assert digits[0] in "23456", f"Medicare first digit must be 2-6, got {digits[0]}"
    weights = [1, 3, 7, 9, 1, 3, 7, 9]
    total = sum(int(digits[i]) * weights[i] for i in range(8))
    assert total % 10 == int(digits[8]), f"Medicare check digit mismatch"


def test_brazil_cpf():
    assert br_cpf_valid(DEMO_PII["BR"]["cpf"]), f"CPF {DEMO_PII['BR']['cpf']} fails checksum"


def test_malaysia_mykad_format():
    """MyKad: YYMMDD-PB-#### format, no checksum — validate structure."""
    mykad = DEMO_PII["MY"]["mykad"]
    digits = mykad.replace("-", "")
    assert len(digits) == 12 and digits.isdigit(), f"MyKad must be 12 digits, got {len(digits)}"
    # Validate date of birth (first 6 digits)
    yy, mm, dd = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
    assert 1 <= mm <= 12, f"MyKad month {mm} invalid"
    assert 1 <= dd <= 31, f"MyKad day {dd} invalid"
    # Validate place-of-birth code (digits 7-8), 01-16 = Malaysian states
    pb = int(digits[6:8])
    valid_pb = set(range(1, 17)) | set(range(21, 60)) | set(range(60, 100))
    assert pb in valid_pb, f"MyKad PB code {pb} invalid"

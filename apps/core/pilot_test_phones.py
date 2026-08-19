"""Known non-pilot phone numbers — internal/test accounts to exclude from pilot metrics.

Shared by anything that reports on "real" pilot shop activity (pilot_audit command,
admin dashboard, health alerting) so the exclusion list doesn't drift between them.
"""

PILOT_TEST_PHONES = {
    "+27644178150",
    "+27610869293",
    "+27641295093",  # Admin — Brighton's own test/admin number
    "+263789848070",  # Test QA
    "+27794979611",  # Gwenyambira & Sons — Kundai/internal test
    "+260954312554",  # Innocent — BOQ contact, not a pilot shop
    "+27746764737",  # Mukusakusa — Brighton's friend in Cape Town, not a pilot shop (see discussion #167)
    "+263786559354",  # Chibuwe — Bradley's own test account, not a pilot shop
}

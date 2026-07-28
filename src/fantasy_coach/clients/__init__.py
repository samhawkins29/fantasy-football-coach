"""Clients subpackage — reserved for M2 (the Yahoo Fantasy read/write client).

M2 will build its yfpy-backed reader and raw-httpx escape hatch on top of
:func:`fantasy_coach.auth.get_authed_client`, which already handles bearer
injection and transparent token refresh. Intentionally empty in M1.
"""

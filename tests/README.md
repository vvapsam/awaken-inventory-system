# Commission engine tests

    python3 -m pytest tests/test_commissions.py -q

`conf_sample.py` holds the seven coach rates and two delegators used by the
tests. In the app these come from the database; here they are fixtures so the
engine can be tested without one.

No Rezerv export is committed — the CSV contains customer names.

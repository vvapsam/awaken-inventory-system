from decimal import Decimal
from app.commissions import CoachRate, Delegator, Config, Settings, FLAT, PERCENT

D = Decimal
OVERRIDE = frozenset({"drop-in", "awaken force"})

RATES = {
    "Anjo R":    CoachRate("Anjo", FLAT, D("750"), OVERRIDE, PERCENT, D("0.50")),
    "JC S":      CoachRate("JC",   FLAT, D("750"), OVERRIDE, PERCENT, D("0.50")),
    "Rick F":    CoachRate("Ric",     PERCENT, D("0.50")),
    "Julio D":   CoachRate("Julio",   PERCENT, D("0.70")),
    "AR M":      CoachRate("AR",      PERCENT, D("0.40")),
    "Joseph J":  CoachRate("Joseph",  PERCENT, D("0.40")),
    "Laurent J": CoachRate("Laurent", PERCENT, D("0.40")),
}

DELEGATORS = [
    Delegator("Gab Rosario",   frozenset({"GR"}),       D("1000"), D("640")),
    Delegator("Culver Padilla", frozenset({"KP", "CP"}), D("1000"), D("640")),
]

def config(**kw):
    s = Settings(default_delegator="KP")
    for k, v in kw.items():
        setattr(s, k, v)
    return Config(coach_rates=RATES, delegators=DELEGATORS, settings=s)

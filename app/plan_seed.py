"""The planning pack you start from.

Every new plan is a copy of this, not a reference to it — the whole point of a
template is that editing one client's budget cannot reach into another's. It is
JSON rather than Python objects so what is stored on a plan row and what is
shipped here are the same shape, and a plan saved last month still loads after
this file changes.

The numbers are placeholders. They came from the Mini HYROX pack for San Miguel
and are a sane starting point for a corporate event of 100-150 people, not a
quote.
"""
import copy
import json

TEMPLATE_JSON = r"""{
  "decisions": [
    [
      "Event date & rain date",
      "Pick a backup date — outdoor events need a Plan B.",
      ""
    ],
    [
      "Headcount (confirmed)",
      "Drives team count, equipment qty, catering, staffing.",
      ""
    ],
    [
      "Team size & structure",
      "Suggested: teams of 4, relay format. ~30 teams for 120 people.",
      ""
    ],
    [
      "Venue",
      "Sports field, park, car park, or company grounds. Need ~flat, open space.",
      ""
    ],
    [
      "Budget (total)",
      "Covers venue, equipment hire, staff/coaches, catering, prizes, medical.",
      ""
    ],
    [
      "Format",
      "Wave/heat start. See Format & Stations tab.",
      ""
    ],
    [
      "Fitness scaling",
      "Offer RX / scaled / walk options at every station so all levels join.",
      ""
    ],
    [
      "Prizes & theme",
      "Fastest team, best team spirit, best costume, most improved, etc.",
      ""
    ]
  ],
  "checklist": [
    {
      "phase": "1. Concept & planning (8–12 weeks out)"
    },
    {
      "n": 1,
      "t": "Confirm objectives & success criteria",
      "d": "Team bonding, wellness, fun, inclusivity. Define what 'success' looks like.",
      "o": "",
      "due": "8–12 wks",
      "s": 0
    },
    {
      "n": 2,
      "t": "Set date + rain/backup date",
      "d": "Avoid clashes with paydays, holidays, other company events.",
      "o": "",
      "due": "8–12 wks",
      "s": 0
    },
    {
      "n": 3,
      "t": "Confirm headcount & fitness mix",
      "d": "Survey staff: who's in, any injuries/limitations.",
      "o": "",
      "due": "8–12 wks",
      "s": 0
    },
    {
      "n": 4,
      "t": "Set overall budget & get sign-off",
      "d": "Use the Budget tab as a starting point.",
      "o": "",
      "due": "8–12 wks",
      "s": 0
    },
    {
      "n": 5,
      "t": "Decide team structure & format",
      "d": "Teams of 4, relay rotation. See Format tab.",
      "o": "",
      "due": "8–12 wks",
      "s": 0
    },
    {
      "n": 6,
      "t": "Appoint an event lead + core committee",
      "d": "One owner accountable; sub-leads for logistics, comms, safety.",
      "o": "",
      "due": "8–12 wks",
      "s": 0
    },
    {
      "n": 7,
      "t": "Decide fun vs. competitive balance",
      "d": "Fun/accessible: timing optional, spirit prizes matter.",
      "o": "",
      "due": "8–12 wks",
      "s": 0
    },
    {
      "phase": "2. Venue & permits (6–10 weeks out)"
    },
    {
      "n": 8,
      "t": "Shortlist & book outdoor venue",
      "d": "Flat, open ~grass or hard-standing; space for circuit + spectators.",
      "o": "",
      "due": "6–10 wks",
      "s": 1
    },
    {
      "n": 9,
      "t": "Check ground surface & drainage",
      "d": "Avoid areas that flood or churn to mud; sled work needs firm ground.",
      "o": "",
      "due": "6–10 wks",
      "s": 0
    },
    {
      "n": 10,
      "t": "Secure permits / permissions",
      "d": "Council/park permit, company grounds approval, noise if using PA.",
      "o": "",
      "due": "6–10 wks",
      "s": 0
    },
    {
      "n": 11,
      "t": "Confirm power, water & toilet access",
      "d": "Generator or mains for PA/timing; drinking water; toilets/portaloos.",
      "o": "",
      "due": "6–10 wks",
      "s": 0
    },
    {
      "n": 12,
      "t": "Plan parking & site access",
      "d": "For attendees, equipment delivery, ambulance access.",
      "o": "",
      "due": "6–10 wks",
      "s": 0
    },
    {
      "n": 13,
      "t": "Arrange wet-weather cover / gazebos",
      "d": "Registration, first aid, catering and equipment must stay dry.",
      "o": "confirm if covered court",
      "due": "6–10 wks",
      "s": 0
    },
    {
      "n": 14,
      "t": "Get public liability insurance / check venue's",
      "d": "Confirm event is covered; keep certificate on file.",
      "o": "",
      "due": "6–10 wks",
      "s": 0
    },
    {
      "n": 15,
      "t": "Do a site walk-through & sketch layout",
      "d": "Map out run route, stations, start/finish, spectator zone.",
      "o": "",
      "due": "4–6 wks",
      "s": 0
    },
    {
      "phase": "3. Format & programme (6–8 weeks out)"
    },
    {
      "n": 16,
      "t": "Finalise circuit & station list",
      "d": "See Format tab — scaled runs + 6–8 simple stations.",
      "o": "",
      "due": "6–8 wks",
      "s": 0
    },
    {
      "n": 17,
      "t": "Set distances & reps per fitness level",
      "d": "RX / scaled / walk option at every station.",
      "o": "",
      "due": "6–8 wks",
      "s": 0
    },
    {
      "n": 18,
      "t": "Decide timing method",
      "d": "Manual stopwatch + score sheets, or a timing app. Fun events can skip precise timing.",
      "o": "",
      "due": "6–8 wks",
      "s": 0
    },
    {
      "n": 19,
      "t": "Build heat/wave schedule",
      "d": "Stagger team starts so stations don't bottleneck.",
      "o": "",
      "due": "4–6 wks",
      "s": 0
    },
    {
      "n": 20,
      "t": "Plan warm-up & cool-down sessions",
      "d": "Group warm-up led by a coach reduces injury risk.",
      "o": "",
      "due": "4–6 wks",
      "s": 0
    },
    {
      "n": 21,
      "t": "Design scorecards / team passports",
      "d": "Simple sheet each team carries around the circuit.",
      "o": "",
      "due": "3–4 wks",
      "s": 0
    },
    {
      "phase": "4. Equipment & supplies (4–8 weeks out)"
    },
    {
      "n": 22,
      "t": "Source functional equipment",
      "d": "Sleds, kettlebells, wall balls, rowers/ski ergs, sandbags — hire or borrow.",
      "o": "",
      "due": "4–8 wks",
      "s": 0
    },
    {
      "n": 23,
      "t": "Order signage, cones & markers",
      "d": "Station signs, run markers, direction arrows, start/finish banner.",
      "o": "",
      "due": "4–6 wks",
      "s": 0
    },
    {
      "n": 24,
      "t": "Arrange PA system + microphone + music",
      "d": "For briefings, start signals, energy, announcements.",
      "o": "",
      "due": "4–6 wks",
      "s": 0
    },
    {
      "n": 25,
      "t": "Get first-aid & safety kit",
      "d": "Kits, ice packs, defib if possible, emergency contact sheet.",
      "o": "",
      "due": "4–6 wks",
      "s": 0
    },
    {
      "n": 26,
      "t": "Order hydration & nutrition",
      "d": "Water stations, cups, electrolytes, fruit/snacks.",
      "o": "",
      "due": "3–4 wks",
      "s": 0
    },
    {
      "n": 27,
      "t": "Sort team identifiers",
      "d": "Coloured bibs/t-shirts/wristbands per team.",
      "o": "",
      "due": "3–4 wks",
      "s": 0
    },
    {
      "n": 28,
      "t": "Book prizes, medals & giveaways",
      "d": "Winners + spirit prizes; finisher medals for everyone is a nice touch.",
      "o": "",
      "due": "3–4 wks",
      "s": 0
    },
    {
      "n": 29,
      "t": "Confirm equipment delivery & pickup times",
      "d": "Coordinate with hire company for day-before or morning drop.",
      "o": "",
      "due": "1–2 wks",
      "s": 0
    },
    {
      "phase": "5. Staffing & suppliers (4–6 weeks out)"
    },
    {
      "n": 30,
      "t": "Book coaches / marshals per station",
      "d": "1 marshal per station to demo, count reps & keep it safe.",
      "o": "",
      "due": "4–6 wks",
      "s": 0
    },
    {
      "n": 31,
      "t": "Book qualified first-aider / medic",
      "d": "Mandatory for a physical event this size.",
      "o": "",
      "due": "4–6 wks",
      "s": 0
    },
    {
      "n": 32,
      "t": "Assign registration & check-in team",
      "d": "Fast, friendly check-in avoids a bottleneck at the start.",
      "o": "",
      "due": "3–4 wks",
      "s": 0
    },
    {
      "n": 33,
      "t": "Confirm MC / host",
      "d": "Keeps energy up, runs briefings, announces results.",
      "o": "",
      "due": "3–4 wks",
      "s": 0
    },
    {
      "n": 34,
      "t": "Arrange photographer / videographer",
      "d": "Great for internal comms and next year's promo.",
      "o": "",
      "due": "3–4 wks",
      "s": 0
    },
    {
      "n": 35,
      "t": "Book catering / food truck",
      "d": "Post-race meal or snacks; cover dietary needs.",
      "o": "",
      "due": "3–4 wks",
      "s": 0
    },
    {
      "n": 36,
      "t": "Brief all staff & volunteers",
      "d": "Roles, timings, safety, radios/comms. See Staffing tab.",
      "o": "",
      "due": "1 wk",
      "s": 0
    },
    {
      "phase": "6. Comms & participants (running throughout)"
    },
    {
      "n": 37,
      "t": "Send save-the-date & sign-up form",
      "d": "Capture teams, sizes, t-shirt sizes, dietary & medical notes.",
      "o": "",
      "due": "6–8 wks",
      "s": 0
    },
    {
      "n": 38,
      "t": "Collect health screening / waivers",
      "d": "PAR-Q style form + liability waiver signed by every participant.",
      "o": "",
      "due": "4–6 wks",
      "s": 0
    },
    {
      "n": 39,
      "t": "Share 'what to expect' & training tips",
      "d": "Reduce nerves; encourage all fitness levels; kit list (trainers, water).",
      "o": "",
      "due": "3–4 wks",
      "s": 0
    },
    {
      "n": 40,
      "t": "Confirm teams & captains",
      "d": "Balanced teams; nominate a captain per team.",
      "o": "",
      "due": "2–3 wks",
      "s": 0
    },
    {
      "n": 41,
      "t": "Send final joining instructions",
      "d": "Location, parking, timings, what to bring, weather plan.",
      "o": "",
      "due": "1 wk",
      "s": 0
    },
    {
      "n": 42,
      "t": "Set up spectator / cheer plan",
      "d": "Encourage non-participants to come support.",
      "o": "",
      "due": "1–2 wks",
      "s": 0
    },
    {
      "phase": "7. Safety & risk (4 weeks + on the day)"
    },
    {
      "n": 43,
      "t": "Complete a risk assessment",
      "d": "Document hazards (heat, trips, lifting, weather) & controls. Keep on file.",
      "o": "",
      "due": "4 wks",
      "s": 0
    },
    {
      "n": 44,
      "t": "Plan emergency procedures",
      "d": "Nearest hospital, ambulance access, emergency contacts, incident log.",
      "o": "",
      "due": "2–4 wks",
      "s": 0
    },
    {
      "n": 45,
      "t": "Set weather / heat policy",
      "d": "Thresholds to modify or postpone; extra water on hot days.",
      "o": "",
      "due": "1–2 wks",
      "s": 0
    },
    {
      "n": 46,
      "t": "Confirm insurance & waivers all in place",
      "d": "No waiver = no participation. Double-check before the day.",
      "o": "",
      "due": "1 wk",
      "s": 0
    },
    {
      "n": 47,
      "t": "Brief marshals on safe technique",
      "d": "Correct lifting/sled form; when to stop a participant.",
      "o": "",
      "due": "Day-of",
      "s": 0
    },
    {
      "phase": "8. Event day (day-of)"
    },
    {
      "n": 48,
      "t": "Early setup: stations, signage, timing",
      "d": "Arrive 2–3 hrs early; test PA; mark run route.",
      "o": "",
      "due": "Day-of",
      "s": 0
    },
    {
      "n": 49,
      "t": "Staff & first-aid in position",
      "d": "Everyone briefed and at posts before doors open.",
      "o": "",
      "due": "Day-of",
      "s": 0
    },
    {
      "n": 50,
      "t": "Run check-in & hand out bibs",
      "d": "Waiver check, team packs, wristbands.",
      "o": "",
      "due": "Day-of",
      "s": 0
    },
    {
      "n": 51,
      "t": "Group warm-up + safety briefing",
      "d": "All participants; explain stations, scaling, etiquette.",
      "o": "",
      "due": "Day-of",
      "s": 0
    },
    {
      "n": 52,
      "t": "Run heats & keep energy high",
      "d": "MC on mic, music, marshals counting & cheering.",
      "o": "",
      "due": "Day-of",
      "s": 0
    },
    {
      "n": 53,
      "t": "Hydration & rest breaks",
      "d": "Watch for overheating; keep water flowing.",
      "o": "",
      "due": "Day-of",
      "s": 0
    },
    {
      "n": 54,
      "t": "Collect scores & tally results",
      "d": "Central results desk; double-check before announcing.",
      "o": "",
      "due": "Day-of",
      "s": 0
    },
    {
      "n": 55,
      "t": "Awards, group photo & wrap-up",
      "d": "Celebrate everyone; hand out prizes & finisher medals.",
      "o": "",
      "due": "Day-of",
      "s": 0
    },
    {
      "n": 56,
      "t": "Pack down & equipment return",
      "d": "Return hire gear; leave venue clean; collect lost property.",
      "o": "",
      "due": "Day-of",
      "s": 0
    },
    {
      "phase": "9. Post-event (within 1–2 weeks)"
    },
    {
      "n": 57,
      "t": "Send thank-you + photos to everyone",
      "d": "Share gallery, results, highlights.",
      "o": "",
      "due": "+1 wk",
      "s": 0
    },
    {
      "n": 58,
      "t": "Run a feedback survey",
      "d": "What worked, what to improve, appetite for next time.",
      "o": "",
      "due": "+1 wk",
      "s": 0
    },
    {
      "n": 59,
      "t": "Reconcile budget & pay suppliers",
      "d": "Close out invoices; compare actual vs. planned spend.",
      "o": "",
      "due": "+1–2 wks",
      "s": 0
    },
    {
      "n": 60,
      "t": "Debrief with committee",
      "d": "Capture lessons learned for next year's event.",
      "o": "",
      "due": "+2 wks",
      "s": 0
    }
  ],
  "fmt1": [
    [
      "Short run lap (between workouts)",
      "500 m",
      "300 m",
      "200 m walk/jog"
    ],
    [
      "Ski erg",
      "500 m",
      "300 m",
      "250 m easy"
    ],
    [
      "Sled push",
      "25 m (loaded)",
      "20 m light",
      "Weighted sled walk"
    ],
    [
      "Sled pull",
      "25 m (loaded)",
      "20 m light",
      "Rope pull seated"
    ],
    [
      "Burpee broad jumps",
      "40 m",
      "20 m step-outs",
      "Sit-to-stand ×10"
    ],
    [
      "Row",
      "500 m",
      "300 m",
      "250 m easy"
    ],
    [
      "Farmers carry",
      "100 m",
      "40 m light",
      "20 m light"
    ],
    [
      "Sandbag lunges",
      "20 m",
      "10 m or bodyweight",
      "Bodyweight squats"
    ],
    [
      "Wall balls",
      "50 reps (6/4kg)",
      "50 reps lighter ball",
      "Air squats / squat to box"
    ]
  ],
  "fmt2": [
    [
      "Runs in between",
      "300 m"
    ],
    [
      "Ski erg",
      "250 m"
    ],
    [
      "KB deadlift to high pull",
      "20 reps (12kg M; 8kg F)"
    ],
    [
      "Burpee broad jump",
      "10 m"
    ],
    [
      "Row",
      "250 m"
    ],
    [
      "Farmers carry",
      "40 m"
    ],
    [
      "Walking lunges",
      "20 m BW"
    ],
    [
      "Wall balls",
      "20 reps (6kg M; 4kg F)"
    ]
  ],
  "equip": [
    {
      "grp": "Functional equipment"
    },
    {
      "t": "Weight plates",
      "q": "8 pairs",
      "src": "Borrow",
      "a": "Awaken",
      "got": false
    },
    {
      "t": "Sleds (push/pull)",
      "q": "8",
      "src": "Borrow",
      "a": "Awaken",
      "got": false
    },
    {
      "t": "Rope for sled pull",
      "q": "8",
      "src": "Borrow",
      "a": "Awaken",
      "got": false
    },
    {
      "t": "Race turf",
      "q": "4 rolls",
      "src": "Borrow",
      "a": "",
      "got": false
    },
    {
      "t": "Ski erg or rowing machines",
      "q": "4",
      "src": "Borrow",
      "a": "Awaken",
      "got": false
    },
    {
      "t": "Wall balls (mixed weights)",
      "q": "8",
      "src": "Borrow",
      "a": "Awaken",
      "got": false
    },
    {
      "t": "Kettlebells / dumbbells (light–mod)",
      "q": "8 pairs",
      "src": "Borrow",
      "a": "Awaken",
      "got": false
    },
    {
      "t": "Sandbags (mixed weights)",
      "q": "8",
      "src": "Borrow",
      "a": "Awaken",
      "got": false
    },
    {
      "grp": "Course & signage"
    },
    {
      "t": "Station signs + direction arrows",
      "q": "1 per station",
      "src": "Print/buy",
      "a": "Awaken",
      "got": false
    },
    {
      "t": "Start / finish lane or arch",
      "q": "1",
      "src": "Hire/buy",
      "a": "Awaken",
      "got": false
    },
    {
      "t": "Barrier tape / fencing",
      "q": "As needed",
      "src": "Buy/hire",
      "a": "Awaken",
      "got": false
    },
    {
      "grp": "Tech & timing"
    },
    {
      "t": "PA system + microphone",
      "q": "1",
      "src": "Hire",
      "a": "",
      "got": false
    },
    {
      "t": "Speaker / music source",
      "q": "1",
      "src": "Own/hire",
      "a": "",
      "got": false
    },
    {
      "t": "Stopwatches / timing app",
      "q": "1 per station",
      "src": "Own/app",
      "a": "",
      "got": false
    },
    {
      "t": "Scorecards / team passports",
      "q": "1 per team",
      "src": "Print",
      "a": "",
      "got": false
    },
    {
      "t": "Two-way radios for staff",
      "q": "4–6",
      "src": "Hire",
      "a": "",
      "got": false
    },
    {
      "t": "LED / projector race results",
      "q": "1",
      "src": "Own/borrow",
      "a": "",
      "got": false
    },
    {
      "t": "Generator (if no mains power)",
      "q": "1",
      "src": "Hire",
      "a": "",
      "got": false
    },
    {
      "grp": "Participant & team"
    },
    {
      "t": "Team bibs / t-shirts / wristbands",
      "q": "150 (buffer)",
      "src": "Buy",
      "a": "",
      "got": false
    },
    {
      "t": "Finisher patches",
      "q": "150",
      "src": "Buy",
      "a": "",
      "got": false
    },
    {
      "t": "Prizes for winners & spirit awards",
      "q": "5–8 sets",
      "src": "Buy",
      "a": "",
      "got": false
    },
    {
      "grp": "Hydration & catering"
    },
    {
      "t": "Water dispensers / bottled water",
      "q": "Plenty + spare",
      "src": "Buy/hire",
      "a": "",
      "got": false
    },
    {
      "t": "Cups (compostable)",
      "q": "300+",
      "src": "Buy",
      "a": "",
      "got": false
    },
    {
      "t": "Electrolytes / sports drinks",
      "q": "As needed",
      "src": "Buy",
      "a": "",
      "got": false
    },
    {
      "t": "Fruit / snacks",
      "q": "For 150",
      "src": "Buy",
      "a": "",
      "got": false
    },
    {
      "t": "Post-event meal / food truck",
      "q": "For 150",
      "src": "Book",
      "a": "",
      "got": false
    },
    {
      "t": "Bins & recycling / waste bags",
      "q": "6–8",
      "src": "Buy",
      "a": "",
      "got": false
    },
    {
      "grp": "Safety & admin"
    },
    {
      "t": "First-aid kits",
      "q": "2–3",
      "src": "Buy",
      "a": "",
      "got": false
    },
    {
      "t": "Ice packs",
      "q": "10+",
      "src": "Buy",
      "a": "",
      "got": false
    },
    {
      "t": "Defibrillator (if available)",
      "q": "1",
      "src": "Hire/venue",
      "a": "",
      "got": false
    },
    {
      "t": "Gazebos / shelter",
      "q": "3–5",
      "src": "Hire",
      "a": "",
      "got": false
    },
    {
      "t": "Registration table + chairs",
      "q": "2–3",
      "src": "Venue/hire",
      "a": "",
      "got": false
    },
    {
      "t": "Waivers & printed forms",
      "q": "150",
      "src": "Print",
      "a": "",
      "got": false
    },
    {
      "t": "Sunscreen & spare warm layers",
      "q": "Bulk",
      "src": "Buy",
      "a": "",
      "got": false
    }
  ],
  "staff": [
    [
      "Event lead",
      "Overall owner; makes the calls; keeps to schedule.",
      "1",
      "Awaken"
    ],
    [
      "Logistics lead",
      "Setup, equipment, suppliers, pack-down.",
      "1",
      "Awaken"
    ],
    [
      "Station marshals",
      "Demo movement, count reps, keep form safe, cheer.",
      "1 per station (6–8)",
      "Awaken"
    ],
    [
      "First-aider / medic",
      "Qualified medical cover for the whole event.",
      "1–2",
      "Red Cross"
    ],
    [
      "Registration team",
      "Check-in, waiver check, hand out bibs & packs.",
      "3–4",
      "Awaken"
    ],
    [
      "MC / host",
      "Briefings, start signals, energy, results announcements.",
      "1",
      "Awaken"
    ],
    [
      "Results / timing desk",
      "Collect scorecards, tally, verify before announcing.",
      "2",
      "Awaken"
    ],
    [
      "Hydration / catering steward",
      "Keep water & snacks stocked; manage food.",
      "2–3",
      "SMC"
    ],
    [
      "Photographer / videographer",
      "Capture the day for comms and next year.",
      "1",
      "Awaken"
    ],
    [
      "Floaters / runners",
      "Fill gaps, fetch things, cover breaks.",
      "2–3",
      "Awaken / SMC / Volunteers"
    ]
  ],
  "runsheet": [
    [
      "4:00 AM",
      "Core team arrives; unload & set up stations, signage, PA.",
      "Logistics lead"
    ],
    [
      "5:00 AM",
      "Test PA/music/timing; mark run route; set up registration & first aid.",
      "Logistics + marshals"
    ],
    [
      "6:30 AM",
      "Full staff briefing; everyone to positions.",
      "Event lead"
    ],
    [
      "7:00 AM",
      "Registration opens; waiver check; hand out bibs & team packs.",
      "Registration team"
    ],
    [
      "7:30 AM",
      "Group warm-up + safety & format briefing.",
      "MC + coach"
    ],
    [
      "8:00 AM",
      "Wave 1 starts; subsequent waves every few minutes.",
      "MC / timing desk"
    ],
    [
      "1:00 PM",
      "Tally results; verify scores.",
      "Results desk"
    ],
    [
      "1:30 PM",
      "Awards, spirit prizes, group photo.",
      "MC + photographer"
    ],
    [
      "3:00 PM",
      "Pack down; equipment return; venue clean-up.",
      "Logistics lead"
    ],
    [
      "3:30 PM",
      "Final sweep & lost property; team debrief.",
      "Event lead"
    ]
  ],
  "budget": {
    "contA": 5,
    "contB": 5,
    "must": [
      {
        "name": "Venue hire / permit",
        "notes": "SMC's court",
        "ua": 0,
        "ca": 1,
        "ub": 0,
        "cb": 1
      },
      {
        "name": "Equipment rental",
        "notes": "Sleds, ergs, weights, etc.",
        "ua": 155000,
        "ca": 1,
        "ub": 75000,
        "cb": 1
      },
      {
        "name": "Equipment delivery & installation",
        "notes": "Deliver + set up",
        "ua": 20000,
        "ca": 1,
        "ub": 20000,
        "cb": 1
      },
      {
        "name": "Coaches / marshals",
        "notes": "Per staff for the day",
        "ua": 5000,
        "ca": 8,
        "ub": 5000,
        "cb": 8
      },
      {
        "name": "Staff food / snacks",
        "notes": "For coaches / marshals",
        "ua": 1000,
        "ca": 8,
        "ub": 1000,
        "cb": 8
      },
      {
        "name": "Volunteers food / snacks",
        "notes": "For volunteers",
        "ua": 1000,
        "ca": 4,
        "ub": 1000,
        "cb": 4
      },
      {
        "name": "First-aid / medic cover",
        "notes": "Red Cross",
        "ua": 20000,
        "ca": 1,
        "ub": 20000,
        "cb": 1
      },
      {
        "name": "PA / timing / radios",
        "notes": "Hire",
        "ua": 3000,
        "ca": 1,
        "ub": 3000,
        "cb": 1
      },
      {
        "name": "DJ + sound system",
        "notes": "",
        "ua": 20000,
        "ca": 1,
        "ub": 20000,
        "cb": 1
      },
      {
        "name": "Race wristbands",
        "notes": "Per person",
        "ua": 35,
        "ca": 135,
        "ub": 35,
        "cb": 15
      },
      {
        "name": "Signage & printing",
        "notes": "Signs, scorecards, banners",
        "ua": 2500,
        "ca": 1,
        "ub": 2500,
        "cb": 1
      },
      {
        "name": "Insurance (if not covered)",
        "notes": "Public liability",
        "ua": 0,
        "ca": 1,
        "ub": 0,
        "cb": 1
      }
    ],
    "addon": [
      {
        "name": "Finisher patches",
        "notes": "Per person",
        "ua": 110,
        "ca": 130,
        "ub": 110,
        "cb": 130,
        "on": true
      },
      {
        "name": "Prizes & spirit awards",
        "notes": "Winners + fun categories",
        "ua": 0,
        "ca": 1,
        "ub": 0,
        "cb": 1,
        "on": true
      },
      {
        "name": "Catering / food truck",
        "notes": "Per head",
        "ua": 0,
        "ca": 1,
        "ub": 0,
        "cb": 1,
        "on": true
      },
      {
        "name": "Water / hydration / snacks",
        "notes": "Sponsor / Pocari",
        "ua": 0,
        "ca": 1,
        "ub": 0,
        "cb": 1,
        "on": true
      },
      {
        "name": "Photographer / video",
        "notes": "Half / full day",
        "ua": 25000,
        "ca": 3,
        "ub": 25000,
        "cb": 3,
        "on": true
      },
      {
        "name": "Gazebos / tent",
        "notes": "Wet-weather cover",
        "ua": 0,
        "ca": 1,
        "ub": 0,
        "cb": 1,
        "on": true
      }
    ]
  },
  "scope": {
    "objective": "Deliver a fun, inclusive Mini HYROX team-building race for ~120 San Miguel employees that promotes wellness, collaboration and friendly competition — run safely, on a single day, and within the approved budget.",
    "inscope": [
      "Scaled team relay race: 6–8 functional-fitness stations plus short runs between them",
      "Venue booking, permits and wet-weather cover",
      "Equipment hire / borrow, delivery and on-site setup",
      "Coaching, station marshalling, first-aid cover and MC on the day",
      "Participant comms: sign-ups, waivers, joining instructions",
      "Prizes, finisher patches, photography and post-event wrap-up"
    ],
    "outscope": [
      "Official/competitive HYROX qualification or certified timing",
      "Overnight travel or accommodation for participants",
      "Paid public ticketing or external (non-SMC) participants",
      "Structured training programmes in the weeks before the event"
    ],
    "deliverables": [
      "Confirmed venue, event date + rain date",
      "Final format, station plan and wave/heat schedule",
      "Signed health waiver from every participant",
      "Fully staffed and equipped event day",
      "Results, awards and shared photo gallery",
      "Post-event feedback report + budget reconciliation"
    ],
    "success": [
      "≥90% of registered participants complete the circuit",
      "Zero serious safety incidents",
      "Post-event satisfaction score ≥4 / 5",
      "Delivered within the approved budget",
      "Strong turnout across all fitness levels (RX / scaled / walk)"
    ],
    "assumptions": [
      "~120 participants in teams of 4",
      "SMC covered court is available as the venue",
      "Equipment sourced via Awaken (borrow / hire)",
      "Single-day event with a morning start",
      "Red Cross provides qualified medical cover"
    ],
    "constraints": [
      "Total spend capped by the selected budget option (see Budget tab)",
      "Weather-dependent — outdoor / covered court",
      "Equipment quantities limit how many stations run at once",
      "Staff and volunteer availability on the event day"
    ],
    "stakeholders": [
      [
        "Event sponsor",
        "SMC leadership",
        "Approves objectives & budget; removes blockers"
      ],
      [
        "Event lead",
        "Awaken",
        "Overall delivery and day-of decisions"
      ],
      [
        "Logistics lead",
        "Awaken",
        "Venue, equipment, setup & pack-down"
      ],
      [
        "Medical cover",
        "Red Cross",
        "Participant safety and first aid"
      ],
      [
        "Participants",
        "SMC employees",
        "Compete in teams of 4"
      ]
    ],
    "milestones": [
      [
        "Objectives & budget signed off",
        "8–12 wks out"
      ],
      [
        "Venue & date locked",
        "6–10 wks out"
      ],
      [
        "Format & stations finalised",
        "6–8 wks out"
      ],
      [
        "Sign-ups & waivers closed",
        "2–3 wks out"
      ],
      [
        "Event day",
        "Day 0"
      ],
      [
        "Debrief & budget reconciliation",
        "+2 wks"
      ]
    ]
  },
  "head": 120,
  "selected": "B"
}"""

#: What the pack is called before anybody renames it.
TEMPLATE_NAME = "Mini HYROX Corporate Event"
TEMPLATE_BLURB = ("A scaled, team-based adaptation of the HYROX race, run as a "
                  "corporate team-building day.")


def blank() -> dict:
    """A fresh copy of the template. Never the template itself."""
    return copy.deepcopy(json.loads(TEMPLATE_JSON))


#: The keys a stored plan is expected to carry. A plan saved before a key
#: existed simply gets the template's version of it rather than a crash.
KEYS = tuple(json.loads(TEMPLATE_JSON).keys())


def hydrate(saved) -> dict:
    """A stored plan, with anything missing filled in from the template.

    Old rows outlive the shape of the thing that wrote them. Filling the gap
    from the template means a plan from before a section existed opens with
    that section rather than a broken page.
    """
    out = blank()
    if isinstance(saved, dict):
        for k, v in saved.items():
            out[k] = v
    return out

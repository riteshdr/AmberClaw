"""Ready-made job presets shown in the New job form."""

PRESETS = [
    {
        "id": "gta5_100",
        "name": "GTA 5 — 100 viral shorts",
        "description": "20 streams x 5 clips, one week of sources. Uploads 6/day via the queue.",
        "fields": {
            "topic": "GTA 5",
            "focus": "funniest, hype, or most exciting GTA 5 moments with clean Roman Hinglish captions",
            "count": "20", "clips": "5", "when": "week", "brain": "groq",
            "language": "", "find": True, "auto_upload": True,
        },
    },
    {
        "id": "gta6_hype",
        "name": "GTA 6 — hype & trailer moments",
        "description": "Trending GTA 6 discussion, trailer reactions and reveal clips.",
        "fields": {
            "topic": "GTA 6",
            "focus": "hype, trailer reveal, funny reactions and biggest GTA 6 moments",
            "count": "10", "clips": "3", "when": "week", "brain": "groq",
            "language": "", "find": True, "auto_upload": True,
        },
    },
    {
        "id": "gta5_funny",
        "name": "GTA 5 — funny fails",
        "description": "Lighter daily batch of comedy and rage moments.",
        "fields": {
            "topic": "GTA 5 funny fails",
            "focus": "funny fails, rage quits and ridiculous moments with Hinglish captions",
            "count": "10", "clips": "4", "when": "week", "brain": "groq",
            "language": "", "find": True, "auto_upload": True,
        },
    },
    {
        "id": "daily_mix",
        "name": "Daily Hinglish gaming mix",
        "description": "Small today-only batch to keep the channel active.",
        "fields": {
            "topic": "GTA gaming funny moments",
            "focus": "short, high-energy Hinglish gaming moments",
            "count": "5", "clips": "3", "when": "today", "brain": "groq",
            "language": "", "find": True, "auto_upload": True,
        },
    },
]


def by_id(preset_id):
    for p in PRESETS:
        if p["id"] == preset_id:
            return p
    return None

class ExperienceState:
    def __init__(self):
        self.mode = "idle"
        self.active_mission = None
        self.activity = None
        self.confidence = None
        self.last_event = None

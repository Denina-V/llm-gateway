class FakeClock:
    """A clock the tests drive by hand.

    Sleeping in a test to exercise a timeout makes the suite slow and flaky, so
    every resilience primitive takes an injectable clock and time becomes a
    variable the test controls.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

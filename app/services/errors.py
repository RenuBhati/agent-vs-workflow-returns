from dataclasses import dataclass, field


@dataclass
class Refusal(Exception):
    """A request the server will not carry out.

    `code` is stable and machine-readable (graded and logged); `message` is for
    people reading logs. Assistants turn the code into their own words.
    """

    code: str
    message: str = ""
    data: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code

"""The logical buttons every input profile maps onto."""

from enum import Enum, auto


class Button(Enum):
    """Logical button, mapped from keyboard or gamepad by an input profile."""

    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()
    SELECT = auto()  # Confirm / drill in  (gamepad A, keyboard Return/a)
    CANCEL = auto()  # Back / cancel        (gamepad B, keyboard b/Escape/Backspace)
    PLAYPAUSE = auto()  # Toggle play/pause    (gamepad X, keyboard x/Space)
    REPEAT = auto()  # Cycle repeat mode    (gamepad Y, keyboard y)
    TERMINATE = auto()  # Quit the application (keyboard Escape)
    VOLUME_UP = auto()  # In-app volume up, if enabled (keyboard +, gamepad R)
    VOLUME_DOWN = auto()  # In-app volume down, if enabled (keyboard -, gamepad L)

"""Abstract base class for agent backends."""

from abc import ABC, abstractmethod


class AgentBackend(ABC):
    """Abstract base for managing AI coding agent sessions."""

    @abstractmethod
    def session_exists(self, name: str) -> bool:
        """Check if a session exists.

        Args:
            name: Session name

        Returns:
            True if session exists
        """
        pass

    @abstractmethod
    def get_output(self, name: str, lines: int = 50) -> str:
        """Get recent output from a session.

        Args:
            name: Session name
            lines: Number of lines to capture

        Returns:
            Recent terminal output as string
        """
        pass

    @abstractmethod
    def send_keys(self, name: str, keys: str) -> bool:
        """Send keys to a session WITHOUT Enter.

        Use for keypresses like selecting menu options.

        Args:
            name: Session name
            keys: Keys to send

        Returns:
            True if keys were sent successfully
        """
        pass

    @abstractmethod
    def send_input(self, name: str, text: str) -> bool:
        """Send input to a session (text + Enter).

        Args:
            name: Session name
            text: Text to send (Enter key is appended)

        Returns:
            True if input was sent successfully
        """
        pass

    @abstractmethod
    def list_sessions(self) -> list[str]:
        """List all active sessions.

        Returns:
            List of session names
        """
        pass


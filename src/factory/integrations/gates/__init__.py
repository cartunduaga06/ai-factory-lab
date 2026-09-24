"""Concrete quality gate execution.

Running a gate means running a local process, which requires ``subprocess`` and
therefore belongs here — outside ``domain`` and ``orchestration`` — behind the
:class:`~factory.domain.ports.QualityGateRunner` port.
"""

from factory.integrations.gates.local import LocalQualityGateRunner

__all__ = ["LocalQualityGateRunner"]

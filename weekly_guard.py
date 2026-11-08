
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from config import WEEKLY_LOSS_LIMIT


STATE_FILE = Path(__file__).with_name("weekly_state.json")


def current_week_id():
    now = datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def save_state(state):
    """Guarda el estado usando un archivo temporal."""
    temp_file = STATE_FILE.with_suffix(".tmp")

    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
        file.flush()

    temp_file.replace(STATE_FILE)


def load_state():
    """Carga y valida el registro semanal."""
    if not STATE_FILE.exists():
        return {
            "week_id": current_week_id(),
            "weekly_loss": "0.00",
            "halted": False,
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)

        week_id = state["week_id"]
        loss = Decimal(state["weekly_loss"])
        halted = state["halted"]

        if not isinstance(week_id, str) or not week_id:
            raise ValueError("Semana inválida")

        if not loss.is_finite() or loss < 0:
            raise ValueError("Pérdida semanal inválida")

        if not isinstance(halted, bool):
            raise ValueError("Estado de parada inválido")

        # Si cambió la semana, guardar el nuevo contador.
        new_week = current_week_id()

        if week_id != new_week:
            new_state = {
                "week_id": new_week,
                "weekly_loss": "0.00",
                "halted": False,
            }
            save_state(new_state)
            return new_state

        # El límite alcanzado siempre activa la parada.
        if loss >= WEEKLY_LOSS_LIMIT:
            halted = True

        return {
            "week_id": week_id,
            "weekly_loss": str(loss),
            "halted": halted,
        }

    except (
        OSError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        ArithmeticError,
    ) as exc:
        raise RuntimeError(
            "No se pudo validar el registro semanal. "
            "Detené el bot y revisá weekly_state.json."
        ) from exc


def record_loss(amount):
    """Registra una pérdida realizada y guarda el estado."""
    amount = Decimal(str(amount))

    if not amount.is_finite() or amount <= 0:
        raise ValueError(
            "La pérdida debe ser un importe positivo y finito."
        )

    state = load_state()
    weekly_loss = Decimal(state["weekly_loss"]) + amount

    updated = {
        "week_id": state["week_id"],
        "weekly_loss": str(weekly_loss),
        "halted": (
            state["halted"]
            or weekly_loss >= WEEKLY_LOSS_LIMIT
        ),
    }

    save_state(updated)
    return updated


def trading_allowed():
    """Indica si se permite operar durante la semana actual."""
    state = load_state()

    return (
        not state["halted"]
        and Decimal(state["weekly_loss"]) < WEEKLY_LOSS_LIMIT
    )


if __name__ == "__main__":
    try:
        state = load_state()
        print("Semana UTC:", state["week_id"])
        print("Pérdidas registradas:", state["weekly_loss"])
        print("Límite semanal:", WEEKLY_LOSS_LIMIT)
        print("¿Puede operar?:", trading_allowed())
    except RuntimeError as exc:
        print("ERROR DE SEGURIDAD:", exc)
        raise SystemExit(1)

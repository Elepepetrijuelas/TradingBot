
from decimal import Decimal

from config import CFG, WEEKLY_LOSS_LIMIT
from risk_manager import (
    ZERO,
    TWO,
    calculate_position_size,
    check_trade,
)


def simulate_trade(
    balance: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    exit_price: Decimal,
    weekly_loss: Decimal = ZERO,
) -> dict:
    """Simula una operación larga sin enviar órdenes a ningún exchange."""

    values = (
        balance,
        entry_price,
        stop_price,
        exit_price,
        weekly_loss,
    )

    if not all(value.is_finite() for value in values):
        raise ValueError("Todos los valores deben ser finitos.")

    if exit_price <= ZERO:
        raise ValueError("El precio de salida debe ser positivo.")

    position, estimated_loss, message = calculate_position_size(
        balance=balance,
        entry_price=entry_price,
        stop_price=stop_price,
        weekly_loss=weekly_loss,
    )

    if position <= ZERO:
        return {
            "approved": False,
            "message": message,
        }

    approved, validation_message = check_trade(
        balance=balance,
        position_value=position,
        entry_price=entry_price,
        stop_price=stop_price,
        weekly_loss=weekly_loss,
    )

    if not approved:
        return {
            "approved": False,
            "message": validation_message,
        }

    # Operación larga: se compra y después se vende.
    gross_pnl = position * (exit_price - entry_price) / entry_price

    # Estimación de comisiones y deslizamiento en ambas direcciones.
    cost_rate = TWO * (
        CFG.fee_rate_per_side + CFG.slippage_rate_per_side
    )
    estimated_costs = position * cost_rate
    net_pnl = gross_pnl - estimated_costs

    new_balance = balance + net_pnl
    realized_weekly_loss = weekly_loss + max(ZERO, -net_pnl)

    return {
        "approved": True,
        "message": validation_message,
        "position": position,
        "estimated_loss_at_stop": estimated_loss,
        "gross_pnl": gross_pnl,
        "estimated_costs": estimated_costs,
        "net_pnl": net_pnl,
        "new_balance": new_balance,
        "weekly_loss_after_trade": realized_weekly_loss,
        "weekly_limit_reached": (
            realized_weekly_loss >= WEEKLY_LOSS_LIMIT
        ),
    }


if __name__ == "__main__":
    # Ejemplo ficticio: compra a $100 y venta a $103.
    result = simulate_trade(
        balance=Decimal("20.00"),
        entry_price=Decimal("100.00"),
        stop_price=Decimal("95.00"),
        exit_price=Decimal("103.00"),
        weekly_loss=Decimal("0.00"),
    )

    print("=== SIMULADOR DE TRADING ===")
    print("Modo: SIMULACIÓN; no envía órdenes reales.")

    if not result["approved"]:
        print("Operación rechazada:", result["message"])
    else:
        print(result["message"])
        print(f"Posición simulada: ${result['position']:.4f}")
        print(
            "Pérdida estimada al stop: "
            f"${result['estimated_loss_at_stop']:.4f}"
        )
        print(f"Ganancia/pérdida bruta: ${result['gross_pnl']:.4f}")
        print(f"Costes estimados: ${result['estimated_costs']:.4f}")
        print(f"Resultado neto estimado: ${result['net_pnl']:.4f}")
        print(f"Balance simulado: ${result['new_balance']:.4f}")
        print(
            "Pérdida semanal acumulada en esta simulación: "
            f"${result['weekly_loss_after_trade']:.4f}"
        )
        print(
            "¿Alcanzó el límite semanal?:",
            "Sí" if result["weekly_limit_reached"] else "No",
        )

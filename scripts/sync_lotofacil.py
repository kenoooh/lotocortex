import datetime as dt
import os
import sys
from typing import Any

import requests


CAIXA_URL = (
    "https://servicebus3.caixa.gov.br/"
    "portaldeloterias/api/lotofacil"
)

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

BRASILIA = dt.timezone(dt.timedelta(hours=-3))


def caixa_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": "LotoCortex-GitHubActions/1.0",
    }


def supabase_headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def convert_date(value: Any) -> str | None:
    text = str(value or "").strip()

    try:
        day, month, year = text.split("/")
        return f"{year}-{month}-{day}"
    except ValueError:
        return None


def get_latest_draw() -> dict[str, Any]:
    response = requests.get(
        CAIXA_URL,
        headers=caixa_headers(),
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()

    contest = int(payload.get("numero") or 0)

    numbers = sorted(
        {
            int(value)
            for value in payload.get("listaDezenas", [])
        }
    )

    if contest <= 0:
        raise RuntimeError("A CAIXA não informou o número do concurso.")

    if len(numbers) != 15:
        raise RuntimeError(
            f"Resultado inválido: esperado 15 dezenas, recebido {len(numbers)}."
        )

    if any(number < 1 or number > 25 for number in numbers):
        raise RuntimeError("A CAIXA retornou dezenas inválidas.")

    return {
        "contest": contest,
        "numbers": numbers,
        "draw_date": convert_date(payload.get("dataApuracao")),
        "next_contest": payload.get("numeroConcursoProximo"),
    }


def supabase_get(path: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=supabase_headers(),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def supabase_write(
    method: str,
    path: str,
    payload: dict[str, Any],
) -> None:
    headers = {
        **supabase_headers(),
        "Prefer": "return=minimal",
    }

    response = requests.request(
        method,
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=headers,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()


def inside_sync_window() -> bool:
    now = dt.datetime.now(BRASILIA)

    # Domingo não possui atualização automática.
    if now.weekday() == 6:
        return False

    start = dt.time(21, 10)
    end = dt.time(22, 0)

    return start <= now.time() <= end


def main() -> None:
    now = dt.datetime.now(BRASILIA)

    if not inside_sync_window():
        print(f"Fora da janela de sincronização: {now.isoformat()}")
        return

    draw = get_latest_draw()

    latest_rows = supabase_get(
        "lottery_draws?select=contest&order=contest.desc&limit=1"
    )

    latest_contest = (
        int(latest_rows[0]["contest"])
        if latest_rows
        else 0
    )

    print(
        f"CAIXA concurso={draw['contest']} "
        f"banco={latest_contest}"
    )

    if draw["contest"] <= latest_contest:
        print("Nenhum concurso novo encontrado.")
        return

    supabase_write(
        "POST",
        "lottery_draws?on_conflict=contest",
        {
            "contest": draw["contest"],
            "numbers": draw["numbers"],
            "draw_date": draw["draw_date"],
            "source": "caixa_github_actions",
        },
    )

    pending_bets = supabase_get(
        "simulated_bets"
        f"?target_contest=eq.{draw['contest']}"
        "&status=eq.awaiting_result"
        "&select=id,numbers"
    )

    drawn_numbers = set(draw["numbers"])

    for bet in pending_bets:
        selected_numbers = {
            int(value)
            for value in (bet.get("numbers") or [])
        }

        hits = len(selected_numbers & drawn_numbers)

        supabase_write(
            "PATCH",
            f"simulated_bets?id=eq.{bet['id']}",
            {
                "status": "checked",
                "result_hits": hits,
                "result_numbers": draw["numbers"],
                "checked_at": dt.datetime.now(
                    dt.timezone.utc
                ).isoformat(),
            },
        )

    print(
        f"Concurso {draw['contest']} salvo com sucesso. "
        f"Apostas conferidas: {len(pending_bets)}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Erro no sincronizador: {error}", file=sys.stderr)
        raise

"""Linha de comando do NoPingeyson.

    python -m nopingeyson                 abre a interface gráfica
    python -m nopingeyson run             roda sem interface (mostra status no terminal)
    python -m nopingeyson medir           só mede os relays por alguns segundos
    python -m nopingeyson keygen          gera uma chave nova para relays e cliente
    python -m nopingeyson boost apply power timer ...   |   boost restore   |   boost status
    python -m nopingeyson relay --port 7000 --key ...    roda o servidor relay
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import time


def _print_status(snap: dict):
    print(f"\n== NoPingeyson  ativo={snap['running']}  {time.strftime('%H:%M:%S')}")
    for p in snap.get("paths", []):
        rtt = f"{p['rtt']:.1f} ms" if p["rtt"] is not None else "—"
        print(f"  {'●' if p['alive'] else '○'} {p['label']:<24} {rtt:>10}  jitter {p['jitter']:.1f}  perda {p['loss']:.0f}%")
    for t in snap.get("targets", []):
        if t["mode"] == "tunneled":
            print(f"  jogo {t['ip']}: saída {t['exit']} via {', '.join(t['paths'])} ~{t['score']} ms "
                  f"(direto {t['direct']}) ↑{t['up']} ↓{t['down']} salvos {t['recovered']}")
        else:
            print(f"  jogo {t['ip']}: {t['mode']}")
    if snap.get("error"):
        print("  ERRO:", snap["error"])


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nopingeyson", description="NoPingeyson: otimizador de rotas para jogos")
    ap.add_argument("--minimizado", action="store_true", help="abre direto na bandeja")
    ap.add_argument("-c", "--config", help="arquivo de configuração (padrão: ./config.json ou %%APPDATA%%\\NoPingeyson)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("gui")
    sub.add_parser("run")
    m = sub.add_parser("medir")
    m.add_argument("-s", "--segundos", type=int, default=6)
    sub.add_parser("keygen")
    b = sub.add_parser("boost")
    b.add_argument("acao", choices=["apply", "restore", "status"])
    b.add_argument("ajustes", nargs="*")
    sub.add_parser("relay", add_help=False)

    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["relay"]:
        from .relay import main as relay_main
        return relay_main(argv[1:])
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if args.cmd == "keygen":
        print(secrets.token_hex(32))
        return

    from .engine import Engine, load_config

    if args.cmd == "boost":
        from .booster import TWEAKS, Booster, is_admin
        bst = Booster()
        if args.acao == "status":
            for k, v in bst.status().items():
                print(f"  [{'x' if v['applied'] else ' '}] {k:<11} {v['label']}")
            return
        if not is_admin():
            print("Aviso: rode como Administrador para os ajustes de sistema funcionarem.")
        if args.acao == "apply":
            names = args.ajustes or [k for k in TWEAKS if k not in ("timer", "priority")]
            for k, v in bst.apply(names).items():
                print(f"  {k}: {v}")
        else:
            for k, v in bst.restore().items():
                print(f"  {k}: {v}")
        return

    cfg = load_config(args.config)
    if args.cmd in (None, "gui"):
        from .gui import run_gui
        return run_gui(cfg, minimized=args.minimizado)

    eng = Engine(cfg)
    res = eng.start(measure_only=(args.cmd == "medir"))
    if res != "ok":
        print("Não foi possível iniciar:", res)
        sys.exit(1)
    try:
        if args.cmd == "medir":
            time.sleep(args.segundos)
            _print_status(eng.snapshot())
        else:
            while True:
                time.sleep(5)
                _print_status(eng.snapshot())
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop()


if __name__ == "__main__":
    main()

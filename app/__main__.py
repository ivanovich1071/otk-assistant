"""Запуск приложения: python -m app

Поднимает локальный сервер и открывает браузер. Наружу не слушает — только
127.0.0.1, приложение однопользовательское.
"""
from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

HOST = "127.0.0.1"
PORT = 8765


def run_window(url: str, port: int) -> None:
    """Оконная версия: тот же сервер, но в своём окне и с системным выбором папки."""
    import webview                                  # noqa: PLC0415

    from app.server import app as server            # noqa: PLC0415

    window = webview.create_window("Ассистент ОТК", url, width=1280, height=820)

    def pick() -> str:
        dialog = getattr(webview, "FileDialog", None)
        kind = dialog.FOLDER if dialog else webview.FOLDER_DIALOG
        chosen = window.create_file_dialog(kind)
        return chosen[0] if chosen else ""

    server.state.picker = pick

    threading.Thread(
        target=lambda: uvicorn.run(server, host=HOST, port=port, log_level="warning"),
        daemon=True,
    ).start()
    webview.start()


def main() -> None:
    ap = argparse.ArgumentParser(description="Ассистент ОТК — локальное приложение")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--desktop", action="store_true", help="в окне вместо браузера")
    ap.add_argument("--reload", action="store_true", help="перезапуск при правке кода")
    args = ap.parse_args()

    url = f"http://{HOST}:{args.port}/"

    if args.desktop:
        try:
            run_window(url, args.port)
            return
        except ImportError:
            print("pywebview не установлен, открываю в браузере: "
                  "pip install pywebview")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    print(f"Ассистент ОТК — {url}\nЗакрыть: Ctrl+C")
    uvicorn.run("app.server:app", host=HOST, port=args.port,
                reload=args.reload, log_level="warning")


if __name__ == "__main__":
    main()

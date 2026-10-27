"""Create the local environment once, without printing the password."""
from pathlib import Path
import secrets

path = Path(__file__).resolve().parents[1] / ".env"
try:
    with path.open("x", encoding="utf-8") as handle:
        path.chmod(0o600)
        handle.write("NEO4J_USER=neo4j\nNEO4J_PASSWORD=" + secrets.token_urlsafe(24) +
                     "\nNEO4J_BOLT_PORT=17687\nNEO4J_HTTP_PORT=17474\n")
    print("Создан локальный .env; пароль сохранён в файле.")
except FileExistsError:
    print(".env уже существует.")

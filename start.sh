#!/bin/sh
# Start the complete local demo from any directory; Docker and Compose are enough.
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir"

usage() {
    cat <<'HELP'
Использование: ./start.sh [команда]
  up       Подготовить настройки, собрать образ, запустить базу и все демонстрации (по умолчанию)
  status   Показать состояние стенда
  stop     Остановить стенд с сохранением данных
  logs     Смотреть журнал Neo4j
  test     Собрать образ и запустить тесты на Neo4j
  cli ...  Выполнить команду supply-risk в контейнере
  help     Показать эту справку
HELP
}

action=${1:-up}
if [ "$#" -gt 0 ]; then
    shift
fi
case "$action" in
    help|-h|--help) usage; exit 0 ;;
    up|status|stop|logs|test|cli) ;;
    *) usage >&2; exit 2 ;;
esac
if [ "$action" != cli ] && [ "$#" -gt 0 ]; then
    usage >&2
    exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
    printf '%s\n' 'Требуется Docker Engine с Docker Compose v2.' >&2
    exit 3
fi
if ! docker compose version >/dev/null 2>&1; then
    printf '%s\n' 'Не найден Docker Compose v2: проверьте команду docker compose version.' >&2
    exit 3
fi
if ! docker info >/dev/null 2>&1; then
    printf '%s\n' 'Docker недоступен: запустите Docker и проверьте доступ к его сокету.' >&2
    exit 3
fi

compose() {
    docker compose "$@"
}

configure() {
    if [ ! -f .env ]; then
        python_image='python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962'
        if ! docker image inspect "$python_image" >/dev/null 2>&1; then
            docker pull "$python_image"
        fi
        docker run --rm --network none --user "$(id -u):$(id -g)" \
            -v "$project_dir:/workspace" -w /workspace --entrypoint python \
            "$python_image" scripts/configure.py
    fi
}

require_config() {
    if [ ! -f .env ]; then
        printf '%s\n' 'Нет локального .env. Для первого запуска выполните ./start.sh.' >&2
        exit 2
    fi
}

case "$action" in
    up)
        configure
        mkdir -p outputs
        printf '%s\n' 'Сборка приложения…'
        compose build app
        printf '%s\n' 'Запуск Neo4j и ожидание готовности…'
        compose up -d --wait neo4j
        printf '%s\n' 'Импорт трёх кейсов, расчёты и экспорт отчётов…'
        if compose run --rm --entrypoint python app scripts/demo.py >outputs/startup.log 2>&1; then
            :
        else
            result=$?
            printf '%s\n' 'Демонстрация завершилась с ошибкой. Последние сообщения:' >&2
            tail -n 40 outputs/startup.log >&2
            exit "$result"
        fi
        http_address=$(compose port neo4j 7474)
        bolt_address=$(compose port neo4j 7687)
        printf '\n%s\n' 'Стенд готов.'
        printf 'Neo4j Browser: http://%s\n' "$http_address"
        printf 'Подключение: bolt://%s\n' "$bolt_address"
        printf '%s\n' 'Логин и пароль: локальный файл .env'
        printf 'Отчёты: %s/outputs/\n' "$project_dir"
        printf '%s\n' 'Компактный граф: outputs/compact_overview.cypher'
        printf '%s\n' 'Журнал демонстрации: outputs/startup.log'
        ;;
    status)
        require_config
        compose ps -a
        ;;
    stop)
        require_config
        compose down
        ;;
    logs)
        require_config
        compose logs --tail=100 -f neo4j
        ;;
    test)
        configure
        mkdir -p outputs
        compose build app
        compose up -d --wait neo4j
        compose run --rm -e RUN_NEO4J_TESTS=1 --entrypoint pytest app -q
        ;;
    cli)
        require_config
        compose run --rm app "$@"
        ;;
esac

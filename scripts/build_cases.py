"""Build reviewed historical cases offline; never fetches leaked databases or live systems."""
from pathlib import Path
import json

from supply_risk.loader import seal, validate

ROOT = Path(__file__).resolve().parents[1]
ACCESSED = "2026-10-26"


def dataset(identifier, title):
    return {"dataset_id": identifier, "version": "1", "synthetic": False, "title": title,
            "entities": [], "rules": [], "dependency_groups": [],
            "parameters": {key: {"value": None, "origin": "unknown", "evidence_ids": []}
                           for key in ("lambda", "T", "mu_b", "mu_r", "volume", "change_scale", "recoverable", "threshold")},
            "severity_policy": {"origin": "assumption", "evidence_ids": [], "categories": {}}}


def add(data, label, identifier, **properties):
    entity = {"id": identifier, "dataset_id": data["dataset_id"], "label": label, **properties}
    data["entities"].append(entity)
    return entity


def source(data, identifier, title, publisher, url, published, excerpt, limitations):
    add(data, "Source", identifier, title=title, publisher=publisher, url=url,
        published_at=published, accessed_at=ACCESSED, excerpt=excerpt, limitations=limitations)


def organization(data, identifier, name, role, scope):
    add(data, "Organization", identifier, name=name, role=role, scope_note=scope, synthetic=False)


def assertion(data, identifier, subject, predicate, obj, date, sources, scope):
    add(data, "Assertion", identifier, subject_id=subject, predicate=predicate, object_id=obj,
        status="source_supported", as_of_date=date, scope_note=scope, evidence_ids=sources,
        review_after_days=None, conflicts_with=[])


def condition(data, identifier, description, sources):
    add(data, "Condition", identifier, description=description, value=True, origin="source_supported",
        evidence_ids=sources, public_source_hint="Датированные сообщения заказчика и поставщика об этом инциденте.")


def rule(data, identifier, assertions, direction, conditions):
    data["rules"].append({"id": identifier, "description": identifier, "assertion_ids": assertions,
                          "direction": direction, "condition_ids": conditions})


def observation(data, identifier, scenario, process, reported, description, metric, sources,
                value=None, unit="качественно", subject=None, kind="business_effect",
                basis="company_statement", limitations="Сведения из заявления источника; денежный ущерб не установлен."):
    add(data, "Observation", identifier, scenario_id=scenario, process_id=process,
        reported_on=reported, description=description, status="source_supported", kind=kind,
        metric=metric, value=value, unit=unit, subject_ref=subject, basis=basis,
        evidence_ids=sources, limitations=limitations)


def save(data):
    validate(seal(data))
    path = ROOT / f"data/{data['dataset_id']}.v1.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rossiya():
    data = dataset("rossiya", "Россия — Леонардо: регистрация и задержки рейсов 28.09.2023")
    day = "2023-09-28"
    organization(data, "rossiya", "Авиакомпания Россия", "customer",
                 "Российский авиаперевозчик; только процессы из сообщений компании от 28.09.2023. Не подменяется всей группой Аэрофлот.")
    organization(data, "sirena", "Сирена-Трэвел", "service_provider",
                 "Российский разработчик/поставщик системы, названный авиакомпанией. Юридическая схема договора, хостинга и связь владения с Ростехом не установлены.")
    add(data, "Service", "leonardo", name="Леонардо: система бронирования и регистрации",
        kind="service", description="Услуга системы, названная в историческом сообщении перевозчика; конкретный технический контур неизвестен.")
    add(data, "Process", "registration", name="Регистрация пассажиров и отправление рейсов России",
        owner_id="rossiya", description="Регистрация в аэропортах маршрутной сети; фактические задержки перечислены отдельно.",
        threshold=None, threshold_origin="unknown", threshold_evidence_ids=[])
    source(data, "src_rossiya_disruption", "Сообщение о затруднении регистрации", "Авиакомпания Россия",
           "https://t.me/rossiya_airlines/980", day,
           "затруднена регистрация в аэропортах маршрутной сети компании",
           "Первичное сообщение; текст прочитан через публичное Telegram embed-представление. Не устанавливает начало сбоя, число пассажиров или текущую архитектуру.")
    source(data, "src_rossiya_delays", "Перечень задержанных рейсов по состоянию на 11:55 мск", "Авиакомпания Россия",
           "https://t.me/rossiya_airlines/981", day,
           "на 11.55 мск задержано отправление рейсов из Шереметьево",
           "Первичное сообщение, опубликовано 08:56:47 UTC. Перечислен 21 рейс; это снимок на 11:55 мск, не итог суток. Задержка рейса не равна длительности отказа системы. Денежные потери не раскрыты.")
    source(data, "src_rossiya_recovery", "Сообщение о восстановлении Леонардо", "Авиакомпания Россия",
           "https://t.me/rossiya_airlines/982", day,
           "Работоспособность системы Leonardo (Сирена-Тревел) восстановлена!",
           "Первичное сообщение опубликовано 09:00:06 UTC. Время публикации не устанавливает точное время восстановления всех процессов и не даёт длительность сбоя.")
    source(data, "src_rostec_ddos", "Сообщение о DDoS-атаке на Леонардо", "Ростех",
           "https://t.me/rostecru/6404", day,
           "на систему «Леонардо» идет массированная DDOS-атака",
           "Первичное заявление участника разработки. Квалификация DDoS дана источником; независимая экспертиза, компрометация серверов и атрибуция злоумышленника не установлены.")
    assertion(data, "r_provider", "sirena", "PROVIDES", "leonardo", day, ["src_rossiya_disruption"],
              "В сообщении перевозчика система обозначена как Leonardo (Сирена-Тревел), поставщик восстанавливает её работу; только контур события 28.09.2023.")
    assertion(data, "r_uses", "registration", "USES", "leonardo", day, ["src_rossiya_disruption", "src_rossiya_delays"],
              "Сбой названной системы затруднил регистрацию; перевозчик связал с ним перечисленные задержки. Текущая зависимость в 2026 году не утверждается.")
    assertion(data, "r_owner", "rossiya", "OWNS_PROCESS", "registration", day, ["src_rossiya_disruption"],
              "Сообщение касается регистрации пассажиров самой авиакомпании Россия.")
    condition(data, "service_disrupted", "28.09.2023 рассматриваемая система была затронута отказом; DDoS указан Ростехом.",
              ["src_rossiya_disruption", "src_rostec_ddos"])
    condition(data, "registration_affected", "28.09.2023 регистрация России использовала затронутую систему и была затруднена.",
              ["src_rossiya_disruption"])
    rule(data, "r_provider_rule", ["r_provider"], "forward", ["service_disrupted"])
    rule(data, "r_consumer_rule", ["r_uses"], "reverse", ["registration_affected"])
    add(data, "Scenario", "R1", name="Леонардо: историческая реконструкция 28.09.2023",
        incident_type="availability", origin_id="sirena", affected_service_id="leonardo",
        rule_ids=["r_provider_rule", "r_consumer_rule"], data_category_ids=[],
        analysis_context="historical_reconstruction", incident_date=day, incident_date_basis="occurred_on", calculation_model="none",
        description="Датированный отказ внешней системы и сообщённые перевозчиком операционные последствия. Исторические задержки хранятся как наблюдения; параметры очереди EDI к регистрации рейсов не применяются.")
    flights = [
        ("FV6311", 60), ("FV6585", 60), ("FV6483", 45), ("FV6075", 60), ("FV6085", 25), ("FV6717", 20),
        ("FV6325", 3), ("FV6353", 6), ("FV6082", 91), ("FV6084", 24), ("FV6013", 27), ("FV6417", 17),
        ("FV6570", 135), ("FV6729", 19), ("FV6733", 15), ("FV6345", 50), ("FV6324", 45),
        ("FV6406", 37), ("FV6424", 22), ("FV6448", 22), ("FV6891", 30),
    ]
    for flight, delay in flights:
        observation(data, f"delay_{flight}", "R1", "registration", day, "Задержка вылета",
                    "flight_delay_minutes", ["src_rossiya_delays"], value=delay, unit="минут", subject=flight,
                    limitations="Снимок на 11:55 мск; не итог суток и не длительность сбоя системы.")
    observation(data, "registration_disruption", "R1", "registration", day,
                "Перевозчик сообщил о затруднении регистрации", "registration_disrupted", ["src_rossiya_disruption"])
    observation(data, "recovery_announced", "R1", "registration", day,
                "Перевозчик сообщил о восстановлении системы", "recovery_announced", ["src_rossiya_recovery"], kind="response")
    return data


def pochta():
    data = dataset("pochta", "Почта России: раскрытие фрагмента данных через аккаунт подрядчика в июле 2022")
    day = "2022-07-29"
    organization(data, "pochta", "Почта России", "customer",
                 "Российская компания; рассматривается только июльский инцидент 2022 года. Октябрьские сообщения о другой базе не объединяются с ним.")
    organization(data, "undisclosed_contractor", "Неназванный подрядчик Почты России", "unidentified_contractor",
                 "Место для участника, существование которого следует из заявления заказчика. Название, юридическое лицо, страна и вид работ не раскрыты; российская регистрация подрядчика не предполагается.")
    add(data, "Service", "contractor_access", name="Подрядное взаимодействие с доступом к фрагменту данных",
        kind="service", description="Зафиксировано существование учётной записи подрядчика. Наименование услуги, протокол доступа и полный периметр неизвестны.")
    add(data, "Process", "shipment_information", name="Обработка и защита сведений об отправлениях",
        owner_id="pochta", description="Аналитическое обозначение затронутого информационного процесса; остановка физической доставки не установлена.",
        threshold=None, threshold_origin="unknown", threshold_evidence_ids=[])
    add(data, "DataCategory", "shipment_fragment", name="Фрагмент сведений об отправлениях",
        description="Компания признала раскрытие фрагмента; точный состав и объём независимо не установлены. Банковские данные, по заявлению компании, не затронуты. Реальные персональные данные не загружаются.")
    source(data, "src_ria_post", "Почта России подтвердила частичную утечку данных абонентов у подрядчика", "РИА Новости",
           "https://ria.ru/20220729/dannye-1806002443.html", day,
           "банковские данные клиентов не пострадали",
           "Прямой комментарий компании агентству; страница прочитана по HTTP, web-представление не загрузилось. Число записей и личность подрядчика не подтверждены; исходные утёкшие базы не изучались.")
    source(data, "src_rbc_post", "Роскомнадзор потребовал от Почты России объяснений из-за утечки данных", "РБК",
           "https://www.rbc.ru/society/30/07/2022/62e469989a794777226b04d4", "2022-07-30",
           "одна из учетных записей подрядчика была скомпрометирована",
           "Прямой комментарий заказчика РБК и сообщение регулятора со ссылкой на ТАСС. Запрос объяснений не означает установленный штраф. Заявление об ограниченном фрагменте не является независимой оценкой размера утечки.")
    assertion(data, "p_provider", "undisclosed_contractor", "PROVIDES", "contractor_access", day,
              ["src_rbc_post", "src_ria_post"], "Заказчик прямо упомянул аккаунт своего подрядчика. Конкретная организация и услуга неизвестны; название узла является обозначением пробела в сведениях.")
    assertion(data, "p_uses", "shipment_information", "USES", "contractor_access", day,
              ["src_rbc_post"], "Контур сведений был доступен через учётную запись подрядчика в пределах признанного фрагмента; не утверждается доступ к полной базе или остановка доставки.")
    assertion(data, "p_owner", "pochta", "OWNS_PROCESS", "shipment_information", day,
              ["src_ria_post"], "Компания подтвердила утечку фрагмента сведений своего информационного контура.")
    for identifier, description in [
        ("account_compromised", "В июльском инциденте 2022 года аккаунт подрядчика был скомпрометирован по заявлению компании."),
        ("account_usable", "В момент раскрытия доступ через аккаунт позволил получить признанный фрагмент; позже компания сообщила о блокировке."),
        ("fragment_present", "Признанный фрагмент данных находился в доступном через этот аккаунт контуре."),
    ]:
        condition(data, identifier, description, ["src_rbc_post"])
    rule(data, "p_access_rule", ["p_provider"], "forward", ["account_compromised", "account_usable", "fragment_present"])
    rule(data, "p_consumer_rule", ["p_uses"], "reverse", [])
    add(data, "Scenario", "P1", name="Почта России: историческая реконструкция июльской утечки 2022",
        incident_type="disclosure", origin_id="undisclosed_contractor", affected_service_id="contractor_access",
        rule_ids=["p_access_rule", "p_consumer_rule"], data_category_ids=["shipment_fragment"],
        analysis_context="historical_reconstruction", incident_date=day, incident_date_basis="reported_on", calculation_model="none",
        description="Разбор признанного заказчиком раскрытия фрагмента через аккаунт подрядчика. 29 июля — дата публичного сообщения, точное время начала атаки неизвестно. Текущее состояние доступа не выводится из истории.")
    observation(data, "fragment_disclosure", "P1", "shipment_information", day,
                "Компания подтвердила раскрытие фрагмента сведений", "data_fragment_disclosed", ["src_ria_post", "src_rbc_post"],
                limitations="Объём и денежный ущерб неизвестны; заявления о миллионах строк не использованы как подтверждённые значения.")
    observation(data, "account_blocked", "P1", "shipment_information", day,
                "Компания сообщила о блокировке скомпрометированного доступа", "account_blocked", ["src_ria_post", "src_rbc_post"], kind="response",
                limitations="Блокировка после обнаружения; не утверждается предотвращение уже произошедшего раскрытия.")
    observation(data, "access_review", "P1", "shipment_information", day,
                "Компания сообщила о перепроверке точек доступа", "access_review", ["src_ria_post", "src_rbc_post"], kind="response")
    observation(data, "regulator_request", "P1", "shipment_information", "2022-07-30",
                "Опубликовано сообщение о запросе Роскомнадзора с требованием детальной информации", "regulator_request", ["src_rbc_post"],
                kind="response", basis="regulator_statement", limitations="Сообщение ведомства процитировано РБК со ссылкой на ТАСС; итог проверки, штраф и точная дата отправки запроса не установлены.")
    return data


if __name__ == "__main__":
    for case in (rossiya(), pochta()):
        save(case)

from tracker.time_contract import (
    TIME_CONTRACT_HEADER,
    get_time_contract,
    log_time_contract_usage,
)


class TimeContractMiddleware:
    """Определяет режим контракта времени (``X-Time-Contract``), ведёт учёт и отдаёт применённый режим.

    Сам режим сериализаторы определяют по запросу напрямую (``get_time_contract``), поэтому
    работа API не зависит от этого middleware; он нужен для метрики и заголовка ответа.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.time_contract = get_time_contract(request)
        response = self.get_response(request)
        response[TIME_CONTRACT_HEADER] = request.time_contract
        log_time_contract_usage(request, response)
        return response

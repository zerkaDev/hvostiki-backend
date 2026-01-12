from celery import shared_task

from tracker.services.ucalles_service import UCallerService


@shared_task
def send_confirmation_code(phone_number, confirmation_code):
    UCallerService().send_call_code(phone_number, confirmation_code)
    return 'Done'
import pytest
from django.contrib.auth import authenticate
from django.urls import reverse
from unittest.mock import patch
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken
from tracker.models import User

@pytest.mark.django_db
class TestAuth:
    def test_send_code_success(self, api_client):
        url = reverse('send_code')
        data = {'phone_number': '79001112233'}
        
        with patch('tracker.tasks.send_confirmation_code.delay') as mock_send:
            response = api_client.post(url, data)
            
        assert response.status_code == 200
        assert response.data['detail'] == 'Код подтверждения отправлен'
        assert User.objects.filter(phone_number='79001112233').exists()
        user = User.objects.get(phone_number='79001112233')
        assert user.confirmation_code != ''

    def test_send_code_invalid_phone(self, api_client):
        url = reverse('send_code')
        data = {'phone_number': '123'}
        response = api_client.post(url, data)
        assert response.status_code == 400

    def test_verify_code_success(self, api_client, user):
        from django.utils import timezone
        user.confirmation_code = '1234'
        user.code_sent_at = timezone.now()
        user.save()
    
        url = reverse('verify_code')
        data = {'phone_number': user.phone_number, 'code': '1234'}
        response = api_client.post(url, data)
    
        assert response.status_code == 200
        assert 'access' in response.data
        assert 'refresh' in response.data
        
        user.refresh_from_db()
        assert user.is_verified is True

    def test_verify_code_invalid(self, api_client, user):
        from django.utils import timezone
        user.confirmation_code = '1234'
        user.code_sent_at = timezone.now()
        user.save()
    
        url = reverse('verify_code')
        data = {'phone_number': user.phone_number, 'code': '0000'}
        response = api_client.post(url, data)
        
        assert response.status_code == 400
        assert 'code' in response.data

    @staticmethod
    def _verify(api_client, user, code='1234'):
        from django.utils import timezone
        user.confirmation_code = '1234'
        user.code_sent_at = timezone.now()
        # Только свои поля: устаревший объект не должен затирать is_verified, выставленный вью.
        user.save(update_fields=['confirmation_code', 'code_sent_at'])

        return api_client.post(
            reverse('verify_code'),
            {'phone_number': user.phone_number, 'code': code},
            format='json',
        )

    def test_verify_code_first_verification_is_new_user(self, api_client, user):
        assert user.is_verified is False

        response = self._verify(api_client, user)

        assert response.status_code == 200
        assert response.data['is_new_user'] is True
        assert response.data['user_id'] == str(user.id)

    def test_verify_code_after_send_code_is_new_user(self, api_client):
        # Запись создаётся уже при отправке кода, но «новым» остаётся тот, кто ещё не подтверждал номер.
        with patch('tracker.tasks.send_confirmation_code.delay'):
            api_client.post(reverse('send_code'), {'phone_number': '79001112299'}, format='json')
        created = User.objects.get(phone_number='79001112299')

        response = api_client.post(
            reverse('verify_code'),
            {'phone_number': '79001112299', 'code': created.confirmation_code},
            format='json',
        )

        assert response.status_code == 200
        assert response.data['is_new_user'] is True
        assert response.data['user_id'] == str(created.id)

    def test_verify_code_repeated_login_is_not_new_user(self, api_client, user):
        first = self._verify(api_client, user)
        second = self._verify(api_client, user)

        assert first.data['is_new_user'] is True
        assert second.status_code == 200
        assert second.data['is_new_user'] is False
        assert second.data['user_id'] == first.data['user_id']

    def test_verify_code_existing_verified_user_is_not_new_user(self, api_client, user):
        user.is_verified = True
        user.save(update_fields=['is_verified'])

        response = self._verify(api_client, user)

        assert response.status_code == 200
        assert response.data['is_new_user'] is False
        assert response.data['user_id'] == str(user.id)

    def test_verify_code_wrong_code_does_not_consume_new_user_flag(self, api_client, user):
        failed = self._verify(api_client, user, code='0000')
        assert failed.status_code == 400
        assert 'is_new_user' not in failed.data
        assert 'user_id' not in failed.data

        user.refresh_from_db()
        assert user.is_verified is False

        succeeded = self._verify(api_client, user)
        assert succeeded.data['is_new_user'] is True

    def test_verify_code_after_account_deletion_is_new_user_again(self, api_client, user):
        first = self._verify(api_client, user)
        assert first.data['is_new_user'] is True

        phone = user.phone_number
        user.delete()

        with patch('tracker.tasks.send_confirmation_code.delay'):
            api_client.post(reverse('send_code'), {'phone_number': phone}, format='json')
        recreated = User.objects.get(phone_number=phone)

        response = api_client.post(
            reverse('verify_code'),
            {'phone_number': phone, 'code': recreated.confirmation_code},
            format='json',
        )

        assert response.status_code == 200
        assert response.data['is_new_user'] is True
        assert response.data['user_id'] == str(recreated.id)
        assert response.data['user_id'] != first.data['user_id']

    def test_verify_code_user_id_matches_profile_id_and_token(self, api_client, user):
        response = self._verify(api_client, user)

        assert response.data['user_id'] == str(AccessToken(response.data['access'])['user_id'])

        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        profile = api_client.get(reverse('profile'))
        assert profile.status_code == 200
        assert profile.data['id'] == response.data['user_id']

    def test_verify_code_response_keeps_token_fields_and_has_no_phone(self, api_client, user):
        response = self._verify(api_client, user)

        assert {'access', 'refresh', 'access_expires', 'refresh_expires'} <= set(response.data)
        assert user.phone_number not in str(response.data.values())

    def test_refresh_response_has_no_signup_fields(self, api_client, user):
        refresh = RefreshToken.for_user(user)

        response = api_client.post(
            reverse('token-refresh'), {'refresh': str(refresh)}, format='json',
        )

        assert response.status_code == 200
        assert 'is_new_user' not in response.data
        assert 'user_id' not in response.data

    def test_refresh_token_success(self, api_client, user):
        refresh = RefreshToken.for_user(user)

        response = api_client.post(
            reverse('token-refresh'),
            {'refresh': str(refresh)},
            format='json',
        )

        assert response.status_code == 200
        assert response.data['refresh'] == str(refresh)
        assert response.data['refresh_expires'] == refresh.payload['exp']

        access = AccessToken(response.data['access'])
        assert access['user_id'] == str(user.id)
        assert response.data['access_expires'] == access.payload['exp']

    def test_refresh_token_rejects_invalid_token(self, api_client):
        response = api_client.post(
            reverse('token-refresh'),
            {'refresh': 'not-a-jwt'},
            format='json',
        )

        assert response.status_code == 401

    def test_refresh_token_requires_token(self, api_client):
        response = api_client.post(reverse('token-refresh'), {}, format='json')

        assert response.status_code == 400

    def test_logout_revokes_submitted_refresh_token(self, auth_client, user):
        refresh = RefreshToken.for_user(user)
        url = reverse('logout')

        response = auth_client.post(
            url,
            {'refresh': str(refresh)},
            format='json',
        )

        assert response.status_code == 200
        assert response.data['detail'] == 'Выход выполнен успешно'

        refresh_response = auth_client.post(
            reverse('token-refresh'),
            {'refresh': str(refresh)},
            format='json',
        )
        assert refresh_response.status_code == 401

    def test_logout_requires_refresh_token(self, auth_client):
        response = auth_client.post(reverse('logout'), {}, format='json')

        assert response.status_code == 400

    def test_logout_is_idempotent(self, auth_client, user):
        refresh = RefreshToken.for_user(user)
        url = reverse('logout')

        first = auth_client.post(url, {'refresh': str(refresh)}, format='json')
        second = auth_client.post(url, {'refresh': str(refresh)}, format='json')

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.data['detail'] == 'Выход выполнен успешно'

    def test_logout_rejects_foreign_refresh_token(self, auth_client, api_client, user):
        other_user = User.objects.create_user(phone_number='79005556677')
        foreign_refresh = RefreshToken.for_user(other_user)

        response = auth_client.post(
            reverse('logout'),
            {'refresh': str(foreign_refresh)},
            format='json',
        )

        assert response.status_code == 403

        # Чужой токен не должен быть отозван
        api_client.force_authenticate(user=None)
        still_valid = api_client.post(
            reverse('token-refresh'),
            {'refresh': str(foreign_refresh)},
            format='json',
        )
        assert still_valid.status_code == 200

    def test_logout_with_invalid_token_returns_401(self, auth_client):
        response = auth_client.post(
            reverse('logout'),
            {'refresh': 'not-a-jwt'},
            format='json',
        )

        assert response.status_code == 401


@pytest.mark.django_db
class TestAdminPhoneLogin:
    """Вход в админку по номеру телефона: формат ввода не должен иметь значения."""

    def _staff(self):
        user = User.objects.create_user(phone_number='79999999999', password='admin-pass-123')
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=['is_staff', 'is_superuser'])
        return user

    @pytest.mark.parametrize('login', ['79999999999', '+79999999999', '89999999999', '+7 (999) 999-99-99'])
    def test_accepts_any_phone_format(self, login):
        user = self._staff()

        assert authenticate(username=login, password='admin-pass-123') == user

    def test_wrong_password_rejected(self):
        self._staff()

        assert authenticate(username='79999999999', password='nope') is None

    def test_unknown_phone_rejected(self):
        assert authenticate(username='+79000000000', password='admin-pass-123') is None


@pytest.mark.django_db
class TestReviewLogin:
    """Режим проверки магазином: вход по фиксированному коду без звонка uCaller."""

    @pytest.fixture(autouse=True)
    def review_mode(self, settings):
        settings.REVIEW_LOGIN_ENABLED = True
        settings.REVIEW_LOGIN_CODE = '0011'
        settings.REVIEW_LOGIN_PHONES = set()
        return settings

    def test_send_code_does_not_call_ucaller(self, api_client):
        with patch('tracker.tasks.send_confirmation_code.delay') as send:
            response = api_client.post(reverse('send_code'), {'phone_number': '79001112233'})

        assert response.status_code == 200
        send.assert_not_called()

    def test_verify_by_review_code(self, api_client):
        api_client.post(reverse('send_code'), {'phone_number': '79001112233'})

        response = api_client.post(
            reverse('verify_code'), {'phone_number': '79001112233', 'code': '0011'},
        )

        assert response.status_code == 200
        assert 'access' in response.data and 'refresh' in response.data
        assert User.objects.get(phone_number='79001112233').is_verified is True

    def test_verify_works_without_prior_send_code(self, api_client):
        response = api_client.post(
            reverse('verify_code'), {'phone_number': '79005550011', 'code': '0011'},
        )

        assert response.status_code == 200
        assert User.objects.filter(phone_number='79005550011').exists()

    def test_other_code_still_rejected(self, api_client):
        api_client.post(reverse('send_code'), {'phone_number': '79001112233'})

        response = api_client.post(
            reverse('verify_code'), {'phone_number': '79001112233', 'code': '0022'},
        )

        assert response.status_code == 400

    def test_allowlist_limits_review_mode(self, api_client, settings):
        settings.REVIEW_LOGIN_PHONES = {'79001112233'}

        with patch('tracker.tasks.send_confirmation_code.delay') as send:
            allowed = api_client.post(reverse('send_code'), {'phone_number': '79001112233'})
            other = api_client.post(reverse('send_code'), {'phone_number': '79005550022'})

        assert allowed.status_code == 200 and other.status_code == 200
        assert send.call_count == 1  # звонок только для номера вне списка
        response = api_client.post(
            reverse('verify_code'), {'phone_number': '79005550022', 'code': '0011'},
        )
        assert response.status_code == 400

    def test_disabled_mode_rejects_review_code(self, api_client, settings):
        settings.REVIEW_LOGIN_ENABLED = False
        api_client.post(reverse('send_code'), {'phone_number': '79001112233'})

        response = api_client.post(
            reverse('verify_code'), {'phone_number': '79001112233', 'code': '0011'},
        )

        assert response.status_code == 400

    def test_empty_code_disables_mode(self, api_client, settings):
        settings.REVIEW_LOGIN_CODE = ''

        response = api_client.post(
            reverse('verify_code'), {'phone_number': '79005550033', 'code': '0011'},
        )

        assert response.status_code == 400

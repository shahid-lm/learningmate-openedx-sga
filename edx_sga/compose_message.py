from openedx.core.djangoapps.ace_common.message import BaseMessageType

class SendEmailToTeacher(BaseMessageType):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.options['transactional'] = True

def compose_activation_email(user, user_registration=None, route_enabled=False, profile_name='', redirect_url=None):
    """
    Construct all the required params for the activation email
    through celery task
    """
    if user_registration is None:
        user_registration = Registration.objects.get(user=user)

    message_context = generate_activation_email_context(user, user_registration)
    message_context.update({
        'confirm_activation_link': _get_activation_confirmation_link(message_context['key'], redirect_url),
        'route_enabled': route_enabled,
        'routed_user': user.username,
        'routed_user_email': user.email,
        'routed_profile_name': profile_name,
    })

    if route_enabled:
        dest_addr = settings.FEATURES['REROUTE_ACTIVATION_EMAIL']
    else:
        dest_addr = user.email

    msg = SendEmailToTeacher().personalize(
        recipient=Recipient(user.id, dest_addr),
        language=preferences_api.get_user_preference(user, LANGUAGE_KEY),
        user_context=message_context,
    )

    return msg
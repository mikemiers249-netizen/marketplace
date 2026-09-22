"""Email confirmation and password recovery for buyers and sellers."""
from flask import Blueprint, render_template, request, flash, redirect, url_for
from flask_login import current_user
from sqlalchemy import func
from app import db
from app.models.users import Buyer, Seller
from app.utils.email_service import configured, identity_for, request_link, resolve_token, consume_token

bp = Blueprint('email_account', __name__, url_prefix='/auth/email')


@bp.after_request
def private_response(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@bp.route('/', methods=['GET', 'POST'])
def manage():
    identity = None
    if current_user.is_authenticated and isinstance(current_user._get_current_object(), (Buyer, Seller)):
        identity = identity_for(current_user._get_current_object())
    if request.method == 'POST':
        if not configured():
            flash('Отправка почты временно недоступна. Попробуйте позже.', 'warning')
        else:
            user = lookup_user()
            if user:
                request_link(user, 'verify')
                db.session.commit()
            flash('Если адрес зарегистрирован и требует подтверждения, письмо будет отправлено. Повторный запрос — через минуту.', 'success')
        return redirect(url_for('.manage'))
    return render_template('auth/email_account.html', mode='verify_request', identity=identity, ready=configured())


def lookup_user():
    kind = request.form.get('user_type', 'buyer')
    if kind not in ('buyer', 'seller'):
        return None
    model = Seller if kind == 'seller' else Buyer
    email = request.form.get('email', '').strip().lower()
    if len(email) > 120 or '@' not in email:
        return None
    # Do not pick an arbitrary account if legacy addresses differ only in case.
    users = model.query.filter(func.lower(model.email) == email).limit(2).all()
    return users[0] if len(users) == 1 else None


@bp.route('/forgot', methods=['GET', 'POST'])
def forgot():
    if request.method == 'POST':
        if not configured():
            flash('Отправка почты временно недоступна. Попробуйте позже.', 'warning')
        else:
            user = lookup_user()
            if user:
                request_link(user, 'reset')
                db.session.commit()
            flash('Если указанный аккаунт существует, письмо для восстановления будет отправлено. Повторный запрос — через минуту.', 'success')
        return redirect(url_for('.forgot'))
    return render_template('auth/email_account.html', mode='reset_request', ready=configured())


@bp.route('/verify/<token>', methods=['GET', 'POST'])
def verify(token):
    valid, _, _ = resolve_token(token, 'verify')
    if not valid:
        return render_template('auth/email_action.html', mode='invalid'), 400
    if request.method == 'POST':
        if consume_token(token, 'verify'):
            return render_template('auth/email_action.html', mode='verified')
        return render_template('auth/email_action.html', mode='invalid'), 400
    # A mail scanner opening GET must not consume the link.
    return render_template('auth/email_action.html', mode='verify')


@bp.route('/reset/<token>', methods=['GET', 'POST'])
def reset(token):
    valid, _, _ = resolve_token(token, 'reset')
    if not valid:
        return render_template('auth/email_action.html', mode='invalid'), 400
    if request.method == 'POST':
        password = request.form.get('password', '')
        if not 8 <= len(password) <= 256 or password != request.form.get('password_confirm'):
            flash('Пароли должны совпадать и содержать от 8 до 256 символов.', 'error')
        elif consume_token(token, 'reset', password):
            return render_template('auth/email_action.html', mode='reset_done')
        else:
            return render_template('auth/email_action.html', mode='invalid'), 400
    return render_template('auth/email_action.html', mode='reset')

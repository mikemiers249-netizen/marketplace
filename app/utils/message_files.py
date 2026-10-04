"""Private chat attachments, including compatibility with existing static URLs."""
from pathlib import Path, PurePosixPath
import secrets

import click
from flask import Flask, abort, current_app, request, send_from_directory, session
from flask_login import current_user
from flask.cli import with_appcontext
from sqlalchemy import and_, or_


class PrivateFilesFlask(Flask):
    def send_static_file(self, filename):
        parts = PurePosixPath(filename.replace('\\', '/')).parts
        if '..' in parts or '.private' in parts:
            abort(404)
        if parts[:2] == ('uploads', 'messages'):
            return serve_attachment(filename)
        return super().send_static_file(filename)


def actor():
    from app.models.users import Buyer, Seller, Admin
    if request.blueprint == 'admin' and session.get('main_admin_authenticated'):
        return 'admin', 0
    if current_user.is_authenticated:
        if isinstance(current_user, Admin):
            return 'admin', 0
        if isinstance(current_user, Seller):
            return 'seller', current_user.id
        if isinstance(current_user, Buyer):
            return 'buyer', current_user.id
    if session.get('main_admin_authenticated'):
        return 'admin', 0
    return None, None


def attachment_name(path):
    if not isinstance(path, str) or '\\' in path:
        return None
    parts = path.split('/')
    if len(parts) != 3 or parts[:2] != ['uploads', 'messages']:
        return None
    name = parts[2]
    if not name or name in ('.', '..') or '\x00' in name:
        return None
    return name


def storage_dir():
    # Same persistent volume as existing uploads; blocked by send_static_file.
    return Path(current_app.config['UPLOAD_FOLDER']) / '.private' / 'messages'


def participant_filter(role, user_id):
    from app.models.communications import Message
    return or_(and_(Message.sender_type == role, Message.sender_id == user_id),
               and_(Message.receiver_type == role, Message.receiver_id == user_id))


def require_order_participant(order):
    role, user_id = actor()
    if not ((role == 'buyer' and order.buyer_id == user_id) or
            (role == 'seller' and order.seller_id == user_id)):
        abort(404)


def validate_attachments(*paths):
    """Prevent a user from attaching somebody else's known URL to a new chat."""
    from app.models.communications import Message, MessageAttachment
    role, user_id = actor()
    for path in paths:
        if not path:
            continue
        if not role or not attachment_name(path):
            abort(400, description='Недопустимое вложение')
        upload = MessageAttachment.query.filter_by(path=path).first()
        if upload:
            allowed = (upload.owner_type, upload.owner_id) == (role, user_id)
        else:
            # Legacy files have no ownership row: only their original sender
            # may reuse them, never a user who merely learned the URL.
            allowed = Message.query.filter(
                or_(Message.image_path == path, Message.file_path == path),
                Message.sender_type == role, Message.sender_id == user_id).first() is not None
        if not allowed:
            abort(403)


def save_attachment(file, extension):
    from app import db
    from app.models.communications import MessageAttachment
    role, user_id = actor()
    if not role:
        abort(401)
    directory = storage_dir()
    directory.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(24) + '.' + extension
    path = 'uploads/messages/' + name
    file.save(directory / name)
    db.session.add(MessageAttachment(path=path, owner_type=role, owner_id=user_id))
    db.session.commit()
    return path


def serve_attachment(path):
    from app.models.communications import Message, MessageAttachment
    role, user_id = actor()
    if not role:
        abort(401)
    name = attachment_name(path)
    if not name:
        abort(404)
    upload = MessageAttachment.query.filter_by(path=path).first()
    own_upload = upload and (upload.owner_type, upload.owner_id) == (role, user_id)
    message = Message.query.filter(
        or_(Message.image_path == path, Message.file_path == path),
        participant_filter(role, user_id)).first()
    if not own_upload and not message:
        abort(404)
    directory = storage_dir()
    # Compatibility until the deployment migration moves all existing files.
    if not (directory / name).is_file():
        directory = Path(current_app.static_folder) / 'uploads' / 'messages'
    response = send_from_directory(directory, name, conditional=True, etag=False, max_age=0)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "sandbox; default-src 'none'"
    return response


@click.command('secure-message-files')
@with_appcontext
def secure_message_files():
    """Move legacy files within the persistent uploads volume, preserving URLs."""
    _move_legacy_files()


def _move_legacy_files():
    source = (Path(current_app.static_folder) / 'uploads' / 'messages').resolve()
    target = storage_dir().resolve()
    target.mkdir(parents=True, exist_ok=True)
    if source == target or source in target.parents or target in source.parents:
        raise click.ClickException('Unsafe attachment storage configuration')
    count = 0
    if source.exists():
        for file in source.iterdir():
            if file.is_symlink() or not file.is_file():
                raise click.ClickException('Unexpected entry in legacy attachment directory')
            destination = target / file.name
            if destination.exists():
                raise click.ClickException('Attachment destination already exists; no file overwritten')
            file.rename(destination)
            count += 1
    click.echo(f'Protected {count} legacy attachments; existing URLs preserved.')

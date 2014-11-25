from superdesk.resource import Resource
from .common import extra_response_fields, item_url, aggregations
from .common import on_create_item, on_create_media_archive, on_update_media_archive, on_delete_media_archive
from .common import get_user
from flask import current_app as app
from werkzeug.exceptions import NotFound
from superdesk import SuperdeskError, get_resource_service
from superdesk.utc import utcnow
from eve.versioning import resolve_document_version
from superdesk.activity import add_activity, ACTIVITY_CREATE, ACTIVITY_UPDATE,\
    ACTIVITY_DELETE
from eve.utils import parse_request, config
from superdesk.services import BaseService
from apps.content import metadata_schema
from apps.common.components.utils import get_component
from apps.item_autosave.components.item_autosave import ItemAutosave
from apps.common.models.base_model import InvalidEtag
from apps.legal_archive.components.legal_archive_proxy import LegalArchiveProxy
from copy import copy


SOURCE = 'archive'


def get_subject(doc1, doc2=None):
    for key in ('headline', 'subject', 'slugline'):
        value = doc1.get(key)
        if not value and doc2:
            value = doc2.get(key)
        if value:
            return value


class ArchiveVersionsResource(Resource):
    schema = metadata_schema
    extra_response_fields = extra_response_fields
    item_url = item_url
    resource_methods = []
    internal_resource = True


class ArchiveVersionsService(BaseService):

    def on_create(self, docs):
        for doc in docs:
            doc['versioncreated'] = utcnow()


class ArchiveResource(Resource):
    schema = {
        'old_version': {
            'type': 'number',
        },
        'last_version': {
            'type': 'number',
        }
    }
    schema.update(metadata_schema)
    extra_response_fields = extra_response_fields
    item_url = item_url
    datasource = {
        'search_backend': 'elastic',
        'aggregations': aggregations,
        'projection': {
            'old_version': 0,
            'last_version': 0
        },
        'default_sort': [('_updated', -1)],
    }
    resource_methods = ['GET', 'POST', 'DELETE']
    versioning = True


class ArchiveService(BaseService):

    def on_create(self, docs):
        on_create_item(docs)

        for doc in docs:
            doc['version_creator'] = doc['original_creator']

    def on_created(self, docs):
        on_create_media_archive()
        get_component(LegalArchiveProxy).create(docs)
        for doc in docs:
            add_activity(ACTIVITY_CREATE, 'added new item {{ type }} about {{ subject }}', item=doc,
                         type=doc['type'], subject=get_subject(doc))

    def on_update(self, updates, original):
        user = get_user()
        lock_user = original.get('lock_user', None)
        force_unlock = updates.get('force_unlock', False)

        original_creator = updates.get('original_creator', None)
        if not original_creator:
            updates['original_creator'] = original['original_creator']

        str_user_id = str(user.get('_id'))
        if lock_user and str(lock_user) != str_user_id and not force_unlock:
            raise SuperdeskError(payload='The item was locked by another user')

        updates['versioncreated'] = utcnow()
        updates['version_creator'] = str_user_id

        if force_unlock:
            del updates['force_unlock']

    def on_updated(self, updates, original):
        get_component(ItemAutosave).clear(original['_id'])
        get_component(LegalArchiveProxy).update(original, updates)
        on_update_media_archive()

        if '_version' in updates:
            updated = copy(original)
            updated.update(updates)
            add_activity(ACTIVITY_UPDATE, 'created new version {{ version }} for item {{ type }} about {{ subject }}',
                         item=updated, version=updates['_version'], subject=get_subject(updates, original))

    def on_replace(self, document, original):
        user = get_user()
        lock_user = original.get('lock_user', None)
        force_unlock = document.get('force_unlock', False)
        user_id = str(user.get('_id'))
        if lock_user and str(lock_user) != user_id and not force_unlock:
            raise SuperdeskError(payload='The item was locked by another user')
        document['versioncreated'] = utcnow()
        document['version_creator'] = user_id
        if force_unlock:
            del document['force_unlock']

    def on_replaced(self, document, original):
        get_component(ItemAutosave).clear(original['_id'])
        on_update_media_archive()

    def on_delete(self, doc):
        '''Delete associated binary files.'''
        if doc and doc.get('renditions'):
            for _name, ref in doc['renditions'].items():
                try:
                    app.media.delete(ref['media'])
                except (KeyError, NotFound):
                    pass

    def on_deleted(self, doc):
        on_delete_media_archive()
        add_activity(ACTIVITY_DELETE, 'removed item {{ type }} about {{ subject }}', item=doc,
                     type=doc['type'], subject=get_subject(doc))

    def replace(self, id, document):
        return self.restore_version(id, document) or \
            super().replace(id, document)

    def restore_version(self, id, doc):
        item_id = id
        old_version = int(doc.get('old_version', 0))
        last_version = int(doc.get('last_version', 0))
        if(not all([item_id, old_version, last_version])):
            return None

        old = get_resource_service('archive_versions').find_one(req=None, _id_document=item_id, _version=old_version)
        if old is None:
            raise SuperdeskError(payload='Invalid version %s' % old_version)

        curr = get_resource_service('archive').find_one(req=None, _id=item_id)
        if curr is None:
            raise SuperdeskError(payload='Invalid item id %s' % item_id)

        if curr[config.VERSION] != last_version:
            raise SuperdeskError(payload='Invalid last version %s' % last_version)
        old['_id'] = old['_id_document']
        old['_updated'] = old['versioncreated'] = utcnow()
        del old['_id_document']

        resolve_document_version(old, 'archive', 'PATCH', curr)
        res = super().replace(id=item_id, document=old)
        del doc['old_version']
        del doc['last_version']
        doc.update(old)
        return res


class AutoSaveResource(Resource):
    endpoint_name = 'archive_autosave'
    item_url = item_url
    schema = {
        '_id': {'type': 'string'}
    }
    schema.update(metadata_schema)
    resource_methods = ['POST']
    item_methods = ['GET', 'PUT', 'PATCH']
    resource_title = endpoint_name


class ArchiveSaveService(BaseService):

    def create(self, docs, **kwargs):
        if not docs:
            raise SuperdeskError('Content is missing', 400)
        req = parse_request(self.datasource)
        try:
            get_component(ItemAutosave).autosave(docs[0]['_id'], docs[0], get_user(required=True), req.if_match)
        except InvalidEtag:
            raise SuperdeskError('Client and server etags don\'t match', 412)
        return [docs[0]['_id']]

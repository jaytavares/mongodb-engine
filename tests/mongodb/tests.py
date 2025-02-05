from django.db import connections
from django.db.utils import DatabaseError
from django.contrib.sites.models import Site

from pymongo.objectid import InvalidId
from pymongo import ASCENDING, DESCENDING
from gridfs import GridOut

from django_mongodb_engine.base import DatabaseWrapper
from django_mongodb_engine.serializer import LazyModelInstance

from .models import *
from .utils import *

class MongoDBEngineTests(TestCase):
    """ Tests for mongodb-engine specific features """

    def test_mongometa(self):
        self.assertEqual(DescendingIndexModel._meta.descending_indexes, ['desc'])

    def test_A_query(self):
        from django_mongodb_engine.query import A
        obj1 = RawModel.objects.create(raw=[{'a' : 1, 'b' : 2}])
        obj2 = RawModel.objects.create(raw=[{'a' : 1, 'b' : 3}])
        self.assertEqualLists(RawModel.objects.filter(raw=A('a', 1)),
                              [obj1, obj2])
        self.assertEqual(RawModel.objects.get(raw=A('b', 2)), obj1)
        self.assertEqual(RawModel.objects.get(raw=A('b', 3)), obj2)

    def test_lazy_model_instance(self):
        l1 = LazyModelInstance(RawModel, 'some-pk')
        l2 = LazyModelInstance(RawModel, 'some-pk')
        self.assertEqual(l1, l2)

        obj = RawModel.objects.create(raw='foobar')
        l3 = LazyModelInstance(RawModel, obj.id)
        self.assertEqual(l3._wrapped, None)
        self.assertEqual(obj, l3)
        self.assertNotEqual(l3._wrapped, None)

    def test_lazy_model_instance_in_list(self):
        from django.conf import settings
        from bson.errors import InvalidDocument

        obj = RawModel(raw=[])
        related = RawModel(raw='foo')
        obj.raw.append(related)
        self.assertRaises(InvalidDocument, obj.save)

        settings.MONGODB_AUTOMATIC_REFERENCING = True
        connections._connections.values()[0]._add_serializer()
        obj.save()
        self.assertNotEqual(related.id, None)
        obj = RawModel.objects.get(id=obj.id)
        self.assertEqual(obj.raw[0]._wrapped, None)
        # query will be done NOW:
        self.assertEqual(obj.raw[0].raw, 'foo')
        self.assertNotEqual(obj.raw[0]._wrapped, None)

    def test_nice_yearmonthday_query_exception(self):
        for x in ('year', 'month', 'day'):
            key = 'date__%s' % x
            self.assertRaisesRegexp(DatabaseError, "MongoDB does not support year/month/day queries",
                                    lambda: DateModel.objects.get(**{key : 1}))

    def test_nice_int_objectid_exception(self):
        msg = "AutoField \(default primary key\) values must be strings " \
              "representing an ObjectId on MongoDB \(got %r instead\)"
        self.assertRaisesRegexp(InvalidId, msg % u'helloworld...',
                                RawModel.objects.create, id='helloworldwhatsup')
        self.assertRaisesRegexp(
            InvalidId, (msg % u'5') + ". Please make sure your SITE_ID contains a valid ObjectId.",
            Site.objects.get, id='5'
        )

    def test_generic_field(self):
        for obj in [['foo'], {'bar' : 'buzz'}]:
            id = RawModel.objects.create(raw=obj).id
            self.assertEqual(RawModel.objects.get(id=id).raw, obj)

    def test_databasewrapper_api(self):
        from pymongo.connection import Connection
        from pymongo.database import Database
        from pymongo.collection import Collection
        from random import shuffle

        if settings.DEBUG:
            from django_mongodb_engine.utils import CollectionDebugWrapper as Collection

        for wrapper in (
            connections['default'],
            DatabaseWrapper(connections['default'].settings_dict.copy())
        ):
            calls = [
                lambda: self.assertIsInstance(wrapper.get_collection('foo'), Collection),
                lambda: self.assertIsInstance(wrapper.database, Database),
                lambda: self.assertIsInstance(wrapper.connection, Connection)
            ]
            shuffle(calls)
            for call in calls:
                call()

class DatabaseOptionTests(TestCase):
    """ Tests for MongoDB-specific database options """

    class custom_database_wrapper(object):
        def __init__(self, settings, **kwargs):
            self.new_wrapper = DatabaseWrapper(
                dict(connections['default'].settings_dict, **settings),
                **kwargs
            )

        def __enter__(self):
            self._old_connection = connections._connections['default']
            connections._connections['default'] = self.new_wrapper
            self.new_wrapper._connect()
            return self.new_wrapper

        def __exit__(self, *exc_info):
            self.new_wrapper.connection.disconnect()
            connections._connections['default'] = self._old_connection

    def test_pymongo_connection_args(self):
        class foodict(dict):
            pass

        with self.custom_database_wrapper({
            'OPTIONS' : {
                'SLAVE_OKAY' : True,
                'NETWORK_TIMEOUT' : 42,
                'TZ_AWARE' : True,
                'DOCUMENT_CLASS' : foodict
            }
        }) as connection:
            for name, value in connection.settings_dict['OPTIONS'].iteritems():
                name = '_Connection__%s' % name.lower()
                self.assertEqual(connection.connection.__dict__[name], value)

    def test_operation_flags(self):
        from textwrap import dedent
        from pymongo.collection import Collection as PyMongoCollection

        def test_setup(flags, **method_kwargs):
            class Collection(PyMongoCollection):
                _method_kwargs = {}
                for name in method_kwargs:
                    exec dedent('''
                    def {0}(self, *a, **k):
                        assert '{0}' not in self._method_kwargs
                        self._method_kwargs['{0}'] = k
                        super(self.__class__, self).{0}(*a, **k)'''.format(name))

            options = {'OPTIONS' : {'OPERATIONS' : flags}}
            with self.custom_database_wrapper(options, collection_class=Collection):
                RawModel.objects.create(raw='foo')
                RawModel.objects.update(raw='foo')
                RawModel.objects.all().delete()

            for name in method_kwargs:
                self.assertEqual(method_kwargs[name],
                                 Collection._method_kwargs[name])

        test_setup({}, save={}, update={'multi' : True}, remove={})
        test_setup(
            {'safe' : True, 'w' : True},
            save={'safe' : True, 'w' : True},
            update={'safe' : True, 'w' : True, 'multi' : True},
            remove={'safe' : True, 'w' : True}
        )
        test_setup(
            {'delete' : {'safe' : True}, 'update' : {}},
            save={},
            update={'multi' : True},
            remove={'safe' : True}
        )
        test_setup(
            {'insert' : {'fsync' : True}, 'delete' : {'w' : True, 'fsync' : True}},
            save={},
            update={'multi' : True},
            remove={'w' : True, 'fsync' : True}
        )

    def test_legacy_flags(self):
        options = {'SAFE_INSERTS' : True, 'WAIT_FOR_SLAVES' : 5}
        with self.custom_database_wrapper(options) as wrapper:
            self.assertTrue(wrapper.operation_flags['save']['safe'])
            self.assertEqual(wrapper.operation_flags['save']['w'], 5)

class IndexTests(TestCase):
    def setUp(self):
        from django.core.management import call_command
        from cStringIO import StringIO
        import sys
        _stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            call_command('sqlindexes', 'mongodb')
        finally:
            sys.stdout = _stdout

    def assertHaveIndex(self, field_name, direction=ASCENDING):
        info = get_collection(IndexTestModel).index_information()
        index_name = field_name + ['_1', '_-1'][direction==DESCENDING]
        self.assertIn(index_name, info)
        self.assertIn((field_name, direction), info[index_name]['key'])

    # Assumes fields as [(name, direction), (name, direction)]
    def assertCompoundIndex(self, fields, model=IndexTestModel):
        info = get_collection(model).index_information()
        index_names = [field[0] + ['_1', '_-1'][field[1]==DESCENDING] for field in fields]
        index_name = "_".join(index_names)
        self.assertIn(index_name, info)
        self.assertEqual(fields, info[index_name]['key'])

    def assertIndexProperty(self, field_name, name, direction=ASCENDING):
        info = get_collection(IndexTestModel).index_information()
        index_name = field_name + ['_1', '_-1'][direction==DESCENDING]
        self.assertTrue(info.get(index_name, {}).get(name, False))

    def test_regular_indexes(self):
        self.assertHaveIndex('regular_index')

    def test_custom_columns(self):
        self.assertHaveIndex('foo')
        self.assertHaveIndex('spam')

    def test_sparse_index(self):
        self.assertHaveIndex('sparse_index')
        self.assertIndexProperty('sparse_index', 'sparse')

        self.assertHaveIndex('sparse_index_unique')
        self.assertIndexProperty('sparse_index_unique', 'sparse')
        self.assertIndexProperty('sparse_index_unique', 'unique')

        self.assertCompoundIndex([('sparse_index_cmp_1', 1), ('sparse_index_cmp_2', 1)])
        self.assertCompoundIndex([('sparse_index_cmp_1', 1), ('sparse_index_cmp_2', 1)])

    def test_compound(self):
        self.assertCompoundIndex([('regular_index', 1), ('custom_column', 1)])
        self.assertCompoundIndex([('a', 1), ('b', -1)], IndexTestModel2)

    def test_foreignkey(self):
        self.assertHaveIndex('foreignkey_index_id')

    def test_descending(self):
        self.assertHaveIndex('descending_index', DESCENDING)
        self.assertHaveIndex('bar', DESCENDING)

class GridFSFieldTests(TestCase):
    def test_empty(self):
        obj = GridFSFieldTestModel.objects.create()
        self.assertEqual(obj.gridfile, None)
        self.assertEqual(obj.gridstring, '')

    def test_gridfile(self):
        fh = open(__file__)
        fh.seek(42)
        obj = GridFSFieldTestModel(gridfile=fh)
        self.assert_(obj.gridfile is fh)
        obj.save()
        self.assert_(obj.gridfile is fh)
        obj = GridFSFieldTestModel.objects.get()
        self.assertIsInstance(obj.gridfile, GridOut)
        fh.seek(42)
        self.assertEqual(obj.gridfile.read(), fh.read())

    def test_deletion(self):
        from gridfs import NoFile
        for field in GridFSFieldTestModel._meta.fields[-2:]:
            GridFSFieldTestModel.objects.create(
                gridstring='foobar', gridfile_nodelete='spam')
            obj = GridFSFieldTestModel.objects.get()
            file_id = field._get_meta(obj).oid
            gridfs = field._get_gridfs(obj)
            obj.delete()
            if field._autodelete:
                self.assertRaises(NoFile, gridfs.get, file_id)
            else:
                self.assertIsInstance(gridfs.get(file_id), GridOut)

    def test_gridstring(self):
        data = open(__file__).read()
        obj = GridFSFieldTestModel(gridstring=data)
        self.assert_(obj.gridstring is data)
        obj.save()
        self.assert_(obj.gridstring is data)
        obj = GridFSFieldTestModel.objects.get()
        self.assertEqual(obj.gridstring, data)

    def test_caching(self):
        """ Make sure GridFS files are read only once """
        GridFSFieldTestModel.objects.create(gridfile=open(__file__))
        obj = GridFSFieldTestModel.objects.get()
        meta = GridFSFieldTestModel._meta.fields[1]._get_meta(obj)
        self.assertEqual(meta.filelike, None)
        obj.gridfile # fetches the file from GridFS
        self.assertNotEqual(meta.filelike, None)
        # from now on, the file should be looked up in the cache.
        # to verify this, we compromise the cache with a sentinel object:
        sentinel = object()
        meta.filelike = sentinel
        self.assertEqual(obj.gridfile, sentinel)

    def test_versioning(self):
        from gridfs import NoFile
        for field in GridFSFieldTestModel._meta.fields[1:3]:
            GridFSFieldTestModel.objects.create(
                gridfile='asd', gridfile_versioned='fgh')
            obj = GridFSFieldTestModel.objects.get()
            first_oid = field._get_meta(obj).oid

            #GridFSFieldTestModel.objects.update(
            #    gridfile='qwe', gridfile_versioned='rty')
            obj.gridfile = 'qwe'
            obj.gridfile_versioned = 'rty'
            obj.save()

            obj = GridFSFieldTestModel.objects.get()
            second_oid = field._get_meta(obj).oid
            assert first_oid != second_oid

            gridfs = field._get_gridfs(obj)
            self.assertIsInstance(gridfs.get(second_oid), GridOut)
            if field._versioning:
                self.assertIsInstance(gridfs.get(first_oid), GridOut)
            else:
                self.assertRaises(NoFile, gridfs.get, first_oid)

            GridFSFieldTestModel.objects.all().delete()

    def test_update(self):
        self.assertRaisesRegexp(
            DatabaseError, "Updates on GridFSFields are not allowed",
            GridFSFieldTestModel.objects.update, gridfile='x'
        )

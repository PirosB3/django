from __future__ import unicode_literals

from bisect import bisect
from collections import OrderedDict, defaultdict
from itertools import chain
import warnings

from django.apps import apps
from django.conf import settings
from django.db.models.fields.related import ManyToManyRel, ManyToManyField
from django.db.models.fields import AutoField, FieldDoesNotExist
from django.db.models.fields.proxy import OrderWrt
from django.utils import six
from django.utils.datastructures import ImmutableList
from django.utils.deprecation import RemovedInDjango20Warning
from django.utils.encoding import force_text, smart_text, python_2_unicode_compatible
from django.utils.functional import cached_property
from django.utils.lru_cache import lru_cache
from django.utils.text import camel_case_to_spaces
from django.utils.translation import activate, deactivate_all, get_language, string_concat

EMPTY_RELATION_TREE = tuple()

IMMUTABLE_WARNING = (
    "The return type of '%s' should never be mutated. If you want to manipulate this list "
    "for your own use, make a copy first."
)

DEFAULT_NAMES = ('verbose_name', 'verbose_name_plural', 'db_table', 'ordering',
                 'unique_together', 'permissions', 'get_latest_by',
                 'order_with_respect_to', 'app_label', 'db_tablespace',
                 'abstract', 'managed', 'proxy', 'swappable', 'auto_created',
                 'index_together', 'apps', 'default_permissions',
                 'select_on_save', 'default_related_name')


class raise_deprecation(object):

    def __init__(self, suggested_alternative):
        self.suggested_alternative = suggested_alternative

    def __call__(self, fn):

        def wrapper(*args, **kwargs):
            warnings.warn(
                "'%s is an unofficial API that has been deprecated. "
                "You may be able to replace it with '%s'" % (
                    fn.__name__,
                    self.suggested_alternative,
                ),
                RemovedInDjango20Warning, stacklevel=2
            )
            return fn(*args, **kwargs)
        return wrapper


def normalize_together(option_together):
    """
    option_together can be either a tuple of tuples, or a single
    tuple of two strings. Normalize it to a tuple of tuples, so that
    calling code can uniformly expect that.
    """
    try:
        if not option_together:
            return ()
        if not isinstance(option_together, (tuple, list)):
            raise TypeError
        first_element = next(iter(option_together))
        if not isinstance(first_element, (tuple, list)):
            option_together = (option_together,)
        # Normalize everything to tuples
        return tuple(tuple(ot) for ot in option_together)
    except TypeError:
        # If the value of option_together isn't valid, return it
        # verbatim; this will be picked up by the check framework later.
        return option_together


def make_immutable_fields_list(name, data):
    return ImmutableList(data, warning=IMMUTABLE_WARNING % name)


@python_2_unicode_compatible
class Options(object):
    def __init__(self, meta, app_label=None):
        self._get_fields_cache = {}
        self._get_field_cache = {}
        self.proxied_children = []
        self.local_fields = []
        self.local_many_to_many = []
        self.virtual_fields = []
        self.model_name = None
        self.verbose_name = None
        self.verbose_name_plural = None
        self.db_table = ''
        self.ordering = []
        self.unique_together = []
        self.index_together = []
        self.select_on_save = False
        self.default_permissions = ('add', 'change', 'delete')
        self.permissions = []
        self.object_name = None
        self.app_label = app_label
        self.get_latest_by = None
        self.order_with_respect_to = None
        self.db_tablespace = settings.DEFAULT_TABLESPACE
        self.meta = meta
        self.pk = None
        self.has_auto_field = False
        self.auto_field = None
        self.abstract = False
        self.managed = True
        self.proxy = False
        # For any class that is a proxy (including automatically created
        # classes for deferred object loading), proxy_for_model tells us
        # which class this model is proxying. Note that proxy_for_model
        # can create a chain of proxy models. For non-proxy models, the
        # variable is always None.
        self.proxy_for_model = None
        # For any non-abstract class, the concrete class is the model
        # in the end of the proxy_for_model chain. In particular, for
        # concrete models, the concrete_model is always the class itself.
        self.concrete_model = None
        self.swappable = None
        self.parents = OrderedDict()
        self.auto_created = False

        # To handle various inheritance situations, we need to track where
        # managers came from (concrete or abstract base classes).
        self.abstract_managers = []
        self.concrete_managers = []

        # List of all lookups defined in ForeignKey 'limit_choices_to' options
        # from *other* models. Needed for some admin checks. Internal use only.
        self.related_fkey_lookups = []

        # A custom app registry to use, if you're making a separate model set.
        self.apps = apps

        self.default_related_name = None

    @lru_cache(maxsize=None)
    def _map_model(self, link):
        direct = not (link.is_reverse_object and link.field.generate_reverse_relation)
        model = link.model if direct else link.parent_model._meta.concrete_model
        if model == self.model:
            model = None
        return link, model

    @lru_cache(maxsize=None)
    def _map_model_details(self, link):
        direct = not (link.is_reverse_object and link.field.generate_reverse_relation)
        model = link.model if direct else link.parent_model._meta.concrete_model
        if model == self.model:
            model = None

        field = link if direct else link.field
        m2m = isinstance(field, ManyToManyField)
        return link, model, direct, m2m

    @property
    def app_config(self):
        # Don't go through get_app_config to avoid triggering imports.
        return self.apps.app_configs.get(self.app_label)

    @property
    def installed(self):
        return self.app_config is not None

    def contribute_to_class(self, cls, name):
        from django.db import connection
        from django.db.backends.utils import truncate_name

        cls._meta = self
        self.model = cls
        # First, construct the default values for these options.
        self.object_name = cls.__name__
        self.model_name = self.object_name.lower()
        self.verbose_name = camel_case_to_spaces(self.object_name)

        # Store the original user-defined values for each option,
        # for use when serializing the model definition
        self.original_attrs = {}

        # Next, apply any overridden values from 'class Meta'.
        if self.meta:
            meta_attrs = self.meta.__dict__.copy()
            for name in self.meta.__dict__:
                # Ignore any private attributes that Django doesn't care about.
                # NOTE: We can't modify a dictionary's contents while looping
                # over it, so we loop over the *original* dictionary instead.
                if name.startswith('_'):
                    del meta_attrs[name]
            for attr_name in DEFAULT_NAMES:
                if attr_name in meta_attrs:
                    setattr(self, attr_name, meta_attrs.pop(attr_name))
                    self.original_attrs[attr_name] = getattr(self, attr_name)
                elif hasattr(self.meta, attr_name):
                    setattr(self, attr_name, getattr(self.meta, attr_name))
                    self.original_attrs[attr_name] = getattr(self, attr_name)

            ut = meta_attrs.pop('unique_together', self.unique_together)
            self.unique_together = normalize_together(ut)

            it = meta_attrs.pop('index_together', self.index_together)
            self.index_together = normalize_together(it)

            # verbose_name_plural is a special case because it uses a 's'
            # by default.
            if self.verbose_name_plural is None:
                self.verbose_name_plural = string_concat(self.verbose_name, 's')

            # Any leftover attributes must be invalid.
            if meta_attrs != {}:
                raise TypeError("'class Meta' got invalid attribute(s): %s" % ','.join(meta_attrs.keys()))
        else:
            self.verbose_name_plural = string_concat(self.verbose_name, 's')
        del self.meta

        # If the db_table wasn't provided, use the app_label + model_name.
        if not self.db_table:
            self.db_table = "%s_%s" % (self.app_label, self.model_name)
            self.db_table = truncate_name(self.db_table, connection.ops.max_name_length())

    def _prepare(self, model):
        if self.order_with_respect_to:
            # The apps registry will not be ready at this point. So
            # we cannot use get_field().
            query = self.order_with_respect_to
            try:
                self.order_with_respect_to = next(
                    f for f in self.get_fields()
                    if f.name == query or f.attname == query
                )
            except StopIteration:
                raise FieldDoesNotExist('%s has no field named %r' % (self.object_name, query))

            self.ordering = ('_order',)
            if not any(isinstance(field, OrderWrt) for field in model._meta.local_fields):
                model.add_to_class('_order', OrderWrt())
        else:
            self.order_with_respect_to = None

        if self.pk is None:
            if self.parents:
                # Promote the first parent link in lieu of adding yet another
                # field.
                field = next(six.itervalues(self.parents))
                # Look for a local field with the same name as the
                # first parent link. If a local field has already been
                # created, use it instead of promoting the parent
                already_created = [fld for fld in self.local_fields if fld.name == field.name]
                if already_created:
                    field = already_created[0]
                field.primary_key = True
                self.setup_pk(field)
            else:
                auto = AutoField(verbose_name='ID', primary_key=True,
                        auto_created=True)
                model.add_to_class('id', auto)

    def add_field(self, field, virtual=False):
        # Insert the given field in the order in which it was created, using
        # the "creation_counter" attribute of the field.
        # Move many-to-many related fields from self.fields into
        # self.many_to_many.
        if virtual:
            self.virtual_fields.append(field)

        elif field.rel and isinstance(field.rel, ManyToManyRel):
            self.local_many_to_many.insert(bisect(self.local_many_to_many, field), field)
        else:
            self.local_fields.insert(bisect(self.local_fields, field), field)
            self.setup_pk(field)
        self._expire_cache()

    def setup_pk(self, field):
        if not self.pk and field.primary_key:
            self.pk = field
            field.serialize = False

    def setup_proxy(self, target):
        """
        Does the internal setup so that the current model is a proxy for
        "target".
        """
        self.pk = target._meta.pk
        self.proxy_for_model = target
        self.db_table = target._meta.db_table

    def __repr__(self):
        return '<Options for %s>' % self.object_name

    def __str__(self):
        return "%s.%s" % (smart_text(self.app_label), smart_text(self.model_name))

    @property
    def verbose_name_raw(self):
        """
        There are a few places where the untranslated verbose name is needed
        (so that we get the same value regardless of currently active
        locale).
        """
        lang = get_language()
        deactivate_all()
        raw = force_text(self.verbose_name)
        activate(lang)
        return raw

    @property
    def swapped(self):
        """
        Has this model been swapped out for another? If so, return the model
        name of the replacement; otherwise, return None.

        For historical reasons, model name lookups using get_model() are
        case insensitive, so we make sure we are case insensitive here.
        """
        if self.swappable:
            model_label = '%s.%s' % (self.app_label, self.model_name)
            swapped_for = getattr(settings, self.swappable, None)
            if swapped_for:
                try:
                    swapped_label, swapped_object = swapped_for.split('.')
                except ValueError:
                    # setting not in the format app_label.model_name
                    # raising ImproperlyConfigured here causes problems with
                    # test cleanup code - instead it is raised in get_user_model
                    # or as part of validation.
                    return swapped_for

                if '%s.%s' % (swapped_label, swapped_object.lower()) not in (None, model_label):
                    return swapped_for
        return None

    @cached_property
    def fields(self):
        """
        Returns a list of all forward fields on the model and its parents excluding
        ManyToManyFields.
        """
        return make_immutable_fields_list("fields", (f for f in self.get_fields()
                                          if not (f.has_many_values and f.has_relation)))

    @cached_property
    def concrete_fields(self):
        """
        Returns a list of all concrete fields on the model and its parents.
        """
        return make_immutable_fields_list("concrete_fields", (f for f in self.fields if f.concrete))

    @cached_property
    def local_concrete_fields(self):
        """
        Returns a list of all concrete fields on the model.
        """
        return make_immutable_fields_list("local_concrete_fields", (f for f in self.local_fields if f.concrete))

    @raise_deprecation(suggested_alternative="get_fields()")
    def get_fields_with_model(self):
        return [self._map_model(f) for f in self.get_fields()]

    @raise_deprecation(suggested_alternative="get_fields()")
    def get_concrete_fields_with_model(self):
        return [self._map_model(f) for f in self.concrete_fields]

    @cached_property
    def many_to_many(self):
        """
        Returns a list of all many to many fields on the model and
        its parents.
        """
        return make_immutable_fields_list("many_to_many", (f for f in self.get_fields()
                                          if f.has_many_values and f.has_relation))

    @cached_property
    def related_objects(self):
        """
        Returns all related objects pointing to the current model.
        The related objects can come from a one-to-one, one-to-many, many-to-many
        field relation type.
        """
        all_related_fields = self.get_fields(forward=False, reverse=True,
                                             include_hidden=True, cache_results=True)
        return make_immutable_fields_list(
            "related_objects",
            (obj for obj in all_related_fields
            if not obj.field.rel.is_hidden() or obj.field.has_many_values)
        )

    @raise_deprecation(suggested_alternative="get_fields()")
    def get_m2m_with_model(self):
        return [self._map_model(f) for f in self.many_to_many]

    @cached_property
    def fields_map(self):
        # call get_fields with export_name_map=true in order to have a field_instance -> names map
        fields = chain(self.get_fields(forward=True, reverse=True, include_hidden=True), self.virtual_fields)
        return dict((field.name, field) for field in fields)

    def get_field(self, field_name, **kwargs):
        """
        Returns a field instance given a field name. The field can be either a forward
        or reverse field
        """
        if not self.apps.ready:

            try:
                #The Apps registry is still not ready, this means get_field() is not able
                #to find related objects that point to this model.
                return next(
                    f for f in self.get_fields()
                    if f.name == field_name
                )
            except StopIteration:
                raise FieldDoesNotExist('%s has no field named %r. The app cache isn\'t '
                                        'ready yet, so if this is a forward field, it won\'t '
                                        'be available yet.' % (self.object_name, field_name))

        fields_map = self.fields_map
        # NOTE: previous get_field API had a many_to_many key. In order to avoid breaking
        # other's implementation we will catch the use of 'many_to_many'.
        if 'many_to_many' in kwargs:

            # If no many_to_many fields are wanted, create a new dictionary with
            # without ManyToManyField instances.
            if kwargs['many_to_many'] is False:
                fields_map = dict((name, field) for name, field in six.iteritems(fields_map)
                                 if not isinstance(field, ManyToManyField))

            # We always want to throw a warning if many_to_many is used regardless
            # of if it alters the return type or not.
            warnings.warn(
                "The 'many_to_many' argument on get_field() is deprecated and will "
                "be removed. Please change your implementation accordingly.",
                RemovedInDjango20Warning
            )

        try:
            # Retreive field instance by name from cached or just-computer field map
            return fields_map[field_name]
        except KeyError:
            raise FieldDoesNotExist('%s has no field named %r' % (self.object_name, field_name))

    @raise_deprecation(suggested_alternative="get_field()")
    def get_field_by_name(self, name):
        return self._map_model_details(self.get_field(name))

    @raise_deprecation(suggested_alternative="field_names")
    def get_all_field_names(self):
        return self.field_names

    @raise_deprecation(suggested_alternative="get_fields()")
    def get_all_related_objects(self, local_only=False, include_hidden=False,
                                include_proxy_eq=False):

        include_parents = local_only is False
        fields = self.get_fields(
            forward=False, reverse=True,
            include_parents=include_parents,
            include_hidden=include_hidden,
        )
        fields = (obj for obj in fields if not isinstance(obj.field, ManyToManyField))

        if include_proxy_eq:
            children = chain.from_iterable(c._relation_tree
                                           for c in self.concrete_model._meta.proxied_children
                                           if c is not self)
            relations = (f.related for f in children
                         if include_hidden or not f.related.field.rel.is_hidden())
            fields = chain(fields, relations)

        return list(fields)

    @raise_deprecation(suggested_alternative="get_fields()")
    def get_all_related_objects_with_model(self, local_only=False, include_hidden=False,
                                           include_proxy_eq=False):
        return [self._map_model(f) for f in
                self.get_all_related_objects(
                    local_only=local_only,
                    include_hidden=include_hidden,
                    include_proxy_eq=include_proxy_eq,
                )]

    @raise_deprecation(suggested_alternative="get_fields()")
    def get_all_related_many_to_many_objects(self, local_only=False):
        fields = self.get_fields(forward=False, reverse=True,
                        include_parents=local_only is not True, include_hidden=True)
        return [obj for obj in fields if isinstance(obj.field, ManyToManyField)]

    @raise_deprecation(suggested_alternative="get_fields()")
    def get_all_related_m2m_objects_with_model(self):
        fields = self.get_fields(forward=False, reverse=True, include_hidden=True)
        return [self._map_model(obj) for obj in fields if isinstance(obj.field, ManyToManyField)]

    def get_base_chain(self, model):
        """
        Returns a list of parent classes leading to 'model' (order from closet
        to most distant ancestor). This has to handle the case were 'model' is
        a grandparent or even more distant relation.
        """
        if not self.parents:
            return None
        if model in self.parents:
            return [model]
        for parent in self.parents:
            res = parent._meta.get_base_chain(model)
            if res:
                res.insert(0, parent)
                return res
        return None

    def get_parent_list(self):
        """
        Returns a list of all the ancestor of this model as a list. Useful for
        determining if something is an ancestor, regardless of lineage.
        """
        result = set()
        for parent in self.parents:
            result.add(parent)
            result.update(parent._meta.get_parent_list())
        return result

    def get_ancestor_link(self, ancestor):
        """
        Returns the field on the current model which points to the given
        "ancestor". This is possible an indirect link (a pointer to a parent
        model, which points, eventually, to the ancestor). Used when
        constructing table joins for model inheritance.

        Returns None if the model isn't an ancestor of this one.
        """
        if ancestor in self.parents:
            return self.parents[ancestor]
        for parent in self.parents:
            # Tries to get a link field from the immediate parent
            parent_link = parent._meta.get_ancestor_link(ancestor)
            if parent_link:
                # In case of a proxied model, the first link
                # of the chain to the ancestor is that parent
                # links
                return self.parents[parent] or parent_link

    def _populate_directed_relation_graph(self):
        """
        This method is used by each model to find it's reverse objects. As this
        method is very expensive and is accessed frequently (it looks up every
        field in a model, in every app), it is computed on first access and then
        is set as a property on every model.
        """
        related_objects_graph = defaultdict(list)

        all_models = self.apps.get_models(include_auto_created=True)
        for model in all_models:

            fields = [model._meta.fields, model._meta.virtual_fields]
            if not model._meta.auto_created:
                fields.append(model._meta.many_to_many)

            fields_with_relations = (
                f for f in chain.from_iterable(fields)
                if f.has_relation and f.related_model != None
            )

            for f in fields_with_relations:
                if not isinstance(f.rel.to, six.string_types):
                    # Set options_instance -> field
                    related_objects_graph[f.rel.to._meta].append(f)

        for model in all_models:
            # Set the relation_tree using the internal __dict__.
            # In this way we avoid calling the cached property.
            # In attribute lookup, __dict__ takes precedence over
            # a data descriptor (such as @cached_property). This
            # means that the _meta._relation_tree is only called
            # if related_objects is not in __dict__.
            related_objects = related_objects_graph[model._meta]

            # If related_objects are empty, it makes sense # to set
            # EMPTY_RELATION_TREE. This will avoid allocating multiple
            # empty relation trees.
            relation_tree = EMPTY_RELATION_TREE
            if related_objects:
                relation_tree = related_objects
            model._meta.__dict__['_relation_tree'] = relation_tree

    @cached_property
    def _relation_tree(self):
        # If cache is not present, populate the cache
        self._populate_directed_relation_graph()
        try:
            return self.__dict__['_relation_tree']
        except KeyError:
            # It may happen, often when the registry is not ready, that
            # a not yet registered model is queried. In this very rare
            # case we simply return an EMPTY_RELATION_TREE.
            # When the registry will be ready, cache will be flushed and
            # this model will be computed properly.
            return EMPTY_RELATION_TREE

    def _expire_cache(self):
        for cache_key in ('fields', 'concrete_fields', 'local_concrete_fields', 'field_names',
                          'related_objects', 'fields_map',):
            try:
                delattr(self, cache_key)
            except AttributeError:
                pass
        self._get_field_cache = {}
        self._get_fields_cache = {}

    def get_fields(self, forward=True, reverse=False,
                   include_parents=True, include_hidden=False, **kwargs):
        """
        Returns a list of fields associated to the model. By default will only return forward fields.
        This can be changed by enabling or disabling field types using the flags available.

        Fields can be any of the following:
        - forward:          fields that are contained on the current model
        - reverse:          fields that point to the current model

        Options can be any of the following:
        - include_parents:        include fields derived from inheritance
        - include_hidden:         include fields that have a related_name that starts with a "+"
        """

        # Creates a cache key composed of all arguments
        cache_results = kwargs.pop('cache_results', True)
        export_name_map = kwargs.pop('export_name_map', False)
        if kwargs:
            raise TypeError("'%s' are invalid keyword arguments" % ', '.join(kwargs.keys()))

        cache_key = (forward, reverse, include_parents, include_hidden, export_name_map)
        try:
            # In order to avoid list manipulation. Always return a shallow copy
            # of the results
            return self._get_fields_cache[cache_key]
        except KeyError:
            pass

        # Using an OrderedDict to preserve the order of insertion. This is fundamental
        # when displaying a ModelForm or django.contrib.admin panel and no specific ordering
        # is provided. For this reason, order of field insertion must be preserved
        fields = OrderedDict()
        options = {
            'include_parents': include_parents,
            'include_hidden': include_hidden,
            'export_name_map': True,
            'cache_results': cache_results,
        }

        if reverse:
            if include_parents:
                parent_list = self.get_parent_list()
                # Recursively call get_fields on each parent, with the same options provided
                # in this call
                for parent in self.parents:
                    for obj, query_name in six.iteritems(parent._meta.get_fields(forward=False, reverse=True,
                                                         **options)):

                        if obj.field.has_many_values:
                            # In order for a reverse ManyToManyRel object to be valid, its creation
                            # counter must be > 0 and must be in the parent list
                            if not (obj.field.creation_counter < 0 and obj.model not in parent_list):
                                fields[obj] = query_name

                        elif not ((obj.field.creation_counter < 0 or obj.field.rel.parent_link)
                                  and obj.model not in parent_list):
                            fields[obj] = query_name

            # Tree is computer once and cached until apps cache is expired. It is composed of
            # a list of fields pointing to the current model from other models.
            # If the model is a proxy model, then we also add the concrete model.
            all_fields = self._relation_tree if not self.proxy else chain(self._relation_tree,
                                                                   self.concrete_model._meta._relation_tree)

            # Pull out all related objects from forward fields
            for field in (f.related for f in all_fields):
                # If hidden fields should be included or the relation
                # is not intentionally hidden, add to the fields dict
                if include_hidden or not field.hidden:
                    fields[field] = field.name

        if forward:
            if include_parents:
                for parent in self.parents:
                    # Extend the fields dict with all the forward fields of each parent.
                    fields.update(parent._meta.get_fields(**options))
            fields.update(
                (field, field.name,)
                for field in chain(self.local_fields, self.local_many_to_many)
            )

        if not export_name_map:
            # By default, fields contains field instances as keys and all possible names
            # if the field instance as values. when get_fields is called, we only want to
            # return field instances, so we just preserve the keys.
            fields = make_immutable_fields_list("get_fields()", fields.keys())

        # Store result into cache for later access
        if cache_results:
            self._get_fields_cache[cache_key] = fields
        # In order to avoid list manipulation. Always
        # return a shallow copy of the results
        return fields

    @cached_property
    def field_names(self):
        """
        Returns a list of all field names in the model, all hidden and proxy fields
        are omitted.
        """
        res = set()
        fields = self.get_fields(reverse=True, export_name_map=True)
        fields.update(
            (field, {field.name, field.attname})
            for field in self.virtual_fields
            if hasattr(field, 'related')
        )
        for _, names in six.iteritems(fields):
            res.update(name for name in names
                       if not (name.endswith('+') or name.endswith('_id')))
        return res

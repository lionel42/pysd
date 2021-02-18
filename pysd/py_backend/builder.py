"""
These elements are used by the translator to construct the model from the
interpreted results. It is technically possible to use these functions to
build a model from scratch. But - it would be rather error prone.

This is code to assemble a pysd model once all of the elements have
been translated from their native language into python compatible syntax.
There should be nothing here that has to know about either vensim or
xmile specific syntax.
"""

import sys
import os.path
import textwrap
import pathlib
import warnings
from io import open
import black

from . import utils

from pysd._version import __version__


# Variable to save identifiers of external objects
build_names = set()

# dictionary for intelligent imports in model file
import_modules = {
    'numpy': False,
    'xarray': False,
    'subs': False,
    'functions': set(),
    'external': set(),
    'utils': set()}


def build(elements, subscript_dict, namespace, outfile_name):
    """
    Actually constructs and writes the python representation of the model

    Parameters
    ----------
    elements: list
        Each element is a dictionary, with the various components needed to assemble
        a model component in python syntax. This will contain multiple entries for
        elements that have multiple definitions in the original file, and which need
        to be combined.

    subscript_dict: dictionary
        A dictionary containing the names of subscript families (dimensions) as keys, and
        a list of the possible positions within that dimension for each value

    namespace: dictionary
        Translation from original model element names (keys) to python safe
        function identifiers (values)

    outfile_name: string
        The name of the file to write the model to.
    """
    # Todo: deal with model level documentation
    # Todo: Make presence of subscript_dict instantiation conditional on usage
    # Todo: Sort elements (alphabetically? group stock funcs?)
    elements = merge_partial_elements(elements)
    functions = [build_element(element, subscript_dict)
                 for element in elements]

    text = '''
    """
    Python model "%(outfile)s"
    Translated using PySD version %(version)s
    """
    from os import path\n''' % {'outfile': os.path.basename(outfile_name),
                                'version': __version__}

    # intelligent import of needed functions and packages
    if import_modules['numpy']:
        text += "    import numpy as np\n"
    if import_modules['xarray']:
        text += "    import xarray as xr\n"
    text += "\n"

    if import_modules['functions']:
        text += "    from pysd.py_backend.functions import %(methods)s\n"\
                % {'methods': ", ".join(import_modules['functions'])}
    if import_modules['external']:
        text += "    from pysd.py_backend.external import %(methods)s\n"\
                % {'methods': ", ".join(import_modules['external'])}
    if import_modules['utils']:
        text += "    from pysd.py_backend.utils import %(methods)s\n"\
                % {'methods': ", ".join(import_modules['utils'])}

    if import_modules['subs']:
        text += "    from pysd import cache, subs\n"
    else:
        # we need to import always cache as it is called in the integration
        text += "    from pysd import cache\n"

    text += '''
    _subscript_dict = %(subscript_dict)s

    _namespace = %(namespace)s

    __pysd_version__ = "%(version)s"

    __data = {
        'scope': None,
        'time': lambda: 0
    }

    _root = path.dirname(__file__)

    def _init_outer_references(data):
        for key in data:
            __data[key] = data[key]

    def time():
        return __data['time']()

    ''' % {'subscript_dict': repr(subscript_dict),
           'namespace': repr(namespace),
           'outfile': os.path.basename(outfile_name),
           'version': __version__}

    text = text.replace('\t', '    ')
    text = textwrap.dedent(text)

    funcs = "%(functions)s" % {'functions': '\n'.join(functions)}
    funcs = funcs.replace('\t', '    ')
    text += funcs

    text = black.format_file_contents(text, fast=True,
                                      mode=black.FileMode())

    # this is needed if more than one model are translated in the same session
    build_names.clear()
    for module in ['numpy', 'xarray', 'subs']:
        import_modules[module] = False
    for module in ['functions', 'external', 'utils']:
        import_modules[module].clear()

    # this is used for testing
    if outfile_name == 'return':
        return text

    with open(outfile_name, 'w', encoding='UTF-8') as out:
        out.write(text)


def build_element(element, subscript_dict):
    """
    Returns a string that has processed a single element dictionary
    Parameters
    ----------
    element: dictionary
        dictionary containing at least the elements:
        - kind: ['constant', 'setup', 'component', 'lookup']
            Different types of elements will be built differently
        - py_expr: string
            An expression that has been converted already into python syntax
        - subs: list of lists
            Each sublist contains coordinates for initialization of a
            particular part of a subscripted function, the list of
            subscripts vensim attaches to an equation

    subscript_dict: dictionary

    Returns
    -------

    """
    if element['kind'] == 'constant':
        cache_type = "@cache.run"
    elif element['kind'] in ['component', 'component_ext_data']:
        cache_type = "@cache.step"
    elif element['kind'] == 'lookup':
        # lookups may be called with different values in a round
        cache_type = ''
    elif element['kind'] in ['setup', 'stateful',
                             'external', 'external_add']:
        # setups only get called once, caching is wasted
        cache_type = ''
    else:
        raise AttributeError("Bad value for 'kind'")

    # check the elements with ADD in their name
    # as these wones are directly added to the
    # external objecets via .add method
    py_expr_no_ADD = ["ADD" not in py_expr for py_expr in element['py_expr']]

    if sum(py_expr_no_ADD) > 1:
        py_expr_i = []
        # need to append true to the end as the next element is checked
        py_expr_no_ADD.append(True)
        for i, (py_expr, subs) in\
          enumerate(zip(element['py_expr'], element['subs'])):
            if py_expr_no_ADD[i] and py_expr_no_ADD[i+1]:
                # rearrange if the element doesn't come from external
                coords = utils.make_coord_dict(subs, subscript_dict,
                                               terse=False)
                dims = list(coords)
                import_modules['utils'].add("rearrange")
                py_expr_i.append('rearrange(%s, %s, %s)' % (
                    py_expr, dims, coords))
            elif py_expr_no_ADD[i] and not py_expr_no_ADD[i+1]:
                # if next element has ADD the current element comes from a
                # external class, no need to rearrange
                py_expr_i.append(py_expr)
        import_modules['utils'].add("xrmerge")
        py_expr = 'xrmerge([%s,])' % (
            ',\n'.join(py_expr_i))
    else:
        py_expr = element['py_expr'][0]

    contents = 'return %s' % py_expr

    element['subs_dec'] = ''
    element['subs_doc'] = 'None'

    if 'subs' in element\
       and element['subs'][0] not in ['', [], None]:
        # for up-dimensioning and reordering
        dims = [utils.find_subscript_name(subscript_dict, sub)
                for sub in element['subs'][0]]
        # We add the list of the subs to the __doc__ of the function
        # this will give more information to the user and make possible
        # to rewrite subscripted values with model.run(params=X) or
        # model.run(initial_condition=(n,x))
        element['subs_doc'] = '%s' % dims
        if element['kind'] in ['component', 'setup']:
            # the decorator is not always necessary as the objects
            # defined as xarrays in the model will have the right
            # dimensions always, we should try to reduce to the
            # maximum when we use it
            # re arrange the python object
            element['subs_dec'] = '@subs(%s, _subscript_dict)' % dims
            import_modules['subs'] = True

    indent = 8
    element.update({'cache': cache_type,
                    'ulines': '-' * len(element['real_name']),
                    'contents': contents.replace('\n',
                                                 '\n' + ' ' * indent)})
                                                 # indent lines 2 onward

    # convert newline indicator and add expected level of indentation
    element['doc'] = element['doc'].replace('\\', '\n').replace('\n', '\n    ')

    if element['kind'] in ['stateful', 'external']:
        func = '''
    %(py_name)s = %(py_expr)s
            ''' % {'py_name': element['py_name'],
                   'py_expr': element['py_expr'][0]}

    elif element['kind'] == 'external_add':
        # external expressions to be added with .add method
        # remove the ADD from the end
        py_name = element['py_name'].split("ADD")[0]
        func = '''
    %(py_name)s%(py_expr)s
            ''' % {'py_name': py_name, 'py_expr': element['py_expr'][0]}

    else:
        sep = '\n' + ' '*10
        if len(element['eqn']) == 1:
            # Original equation in the same line
            element['eqn'] = element['eqn'][0]
        elif len(element['eqn']) > 5:
            # First and last original equations separated by vertical dots
            element['eqn'] = sep + element['eqn'][0] + (sep+'  .')*3\
                             + sep + element['eqn'][-1]
        else:
            # From 2 to 5 equations in different lines
            element['eqn'] = sep + sep.join(element['eqn'])

        func = '''
    %(cache)s
    %(subs_dec)s
    def %(py_name)s(%(arguments)s):
        """
        Real Name: %(real_name)s
        Original Eqn: %(eqn)s
        Units: %(unit)s
        Limits: %(lims)s
        Type: %(kind)s
        Subs: %(subs_doc)s

        %(doc)s
        """
        %(contents)s
        ''' % element

    func = textwrap.dedent(func)

    return func


def merge_partial_elements(element_list):
    """
    merges model elements which collectively all define the model component,
    mostly for multidimensional subscripts

    Parameters
    ----------
    element_list

    Returns
    -------
    """
    outs = dict()  # output data structure

    for element in element_list:
        if element['py_expr'] != "None":  # for
            name = element['py_name']
            if name not in outs:

                # Use 'expr' for Vensim models, and 'eqn' for Xmile
                # (This makes the Vensim equation prettier.)
                eqn = element['expr'] if 'expr' in element else element['eqn']

                outs[name] = {
                    'py_name': element['py_name'],
                    'real_name': element['real_name'],
                    'doc': element['doc'],
                    'py_expr': [element['py_expr']],  # in a list
                    'unit': element['unit'],
                    'subs': [element['subs']],
                    'lims': element['lims'],
                    'eqn': [eqn.replace(r'\ ', '')],
                    'kind': element['kind'],
                    'arguments': element['arguments']
                }

            else:
                eqn = element['expr'] if 'expr' in element else element['eqn']

                outs[name]['doc'] = outs[name]['doc'] or element['doc']
                outs[name]['unit'] = outs[name]['unit'] or element['unit']
                outs[name]['lims'] = outs[name]['lims'] or element['lims']
                outs[name]['eqn'] += [eqn.replace(r'\ ', '')]
                outs[name]['py_expr'] += [element['py_expr']]
                outs[name]['subs'] += [element['subs']]
                outs[name]['arguments'] = element['arguments']

    return list(outs.values())


def add_stock(identifier, expression, initial_condition,
              subs, subscript_dict):
    """
    Creates new model element dictionaries for the model elements associated
    with a stock.

    Parameters
    ----------
    identifier: basestring
        the python-safe name of the stock

    expression: basestring
        The formula which forms the derivative of the stock

    initial_condition: basestring
        Formula which forms the initial condition for the stock

    subs: list of strings
        List of strings of subscript indices that correspond to the
        list of expressions, and collectively define the shape of the output

    subscript_dict: dictionary
        Dictionary describing the possible dimensions of the stock's subscripts

    Returns
    -------
    reference: string
        a string to use in place of the 'INTEG...' pieces in the element
        expression string, a reference to the stateful object
    new_structure: list

        list of additional model element dictionaries. When there are
        subscripts, constructs an external 'init' and 'ddt' function so
        that these can be appropriately aggregated

    """
    import_modules['functions'].add("Integ")

    new_structure = []

    if len(subs) == 0:
        stateful_py_expr = 'Integ(lambda: %s, lambda: %s)' % (
            expression, initial_condition)
    else:
        stateful_py_expr = 'Integ(lambda: _d%s_dt(), lambda: '\
                           '_init_%s())' % (identifier, identifier)

        # following elements not specified in the model file, but must exist
        # create the stock initialization element
        new_structure.append({
            'py_name': '_init_%s' % identifier,
            'real_name': 'Implicit',
            'kind': 'setup',
            'py_expr': initial_condition,
            'subs': subs,
            'doc': 'Provides initial conditions for %s function' % identifier,
            'unit': 'See docs for %s' % identifier,
            'lims': 'None',
            'eqn': 'None',
            'arguments': ''
        })

        new_structure.append({
            'py_name': '_d%s_dt' % identifier,
            'real_name': 'Implicit',
            'kind': 'component',
            'doc': 'Provides derivative for %s function' % identifier,
            'subs': subs,
            'unit': 'See docs for %s' % identifier,
            'lims': 'None',
            'eqn': 'None',
            'py_expr': expression,
            'arguments': ''
        })

    # describe the stateful object
    stateful = {
        'py_name': '_integ_%s' % identifier,
        'real_name': 'Representation of  %s' % identifier,
        'doc': 'Integrates Expression %s' % expression,
        'py_expr': stateful_py_expr,
        'unit': 'None',
        'lims': 'None',
        'eqn': 'None',
        'subs': '',
        'kind': 'stateful',
        'arguments': ''
    }

    new_structure.append(stateful)
    return "%s()" % stateful['py_name'], new_structure


def add_n_delay(identifier, delay_input, delay_time, initial_value, order,
                subs, subscript_dict):
    """
    Creates code to instantiate a stateful 'Delay' object,
    and provides reference to that object's output.

    The name of the stateful object is based upon the passed in parameters,
    so if there are multiple places where identical delay functions are
    referenced, the translated python file will only maintain one stateful
    object, and reference it multiple times.

    Parameters
    ----------
    identifier: basestring
        the python-safe name of the stock

    delay_input: <string>
        Reference to the model component that is the input to the delay

    delay_time: <string>
        Can be a number (in string format) or a reference to another model
        element which will calculate the delay. This is calculated throughout
        the simulation at runtime.

    initial_value: <string>
        This is used to initialize the stocks that are present in the delay.
        We initialize the stocks with equal values so that the outflow in
        the first timestep is equal to this value.

    order: string
        The number of stocks in the delay pipeline. As we construct the
        delays at build time, this must be an integer and cannot be calculated
        from other model components. Anything else will yield a ValueError.

    subs: list of strings
        List of strings of subscript indices that correspond to the
        list of expressions, and collectively define the shape of the output

    subscript_dict: dictionary
        Dictionary describing the possible dimensions of the stock's subscripts

    Returns
    -------
    reference: basestring
        reference to the delay object `__call__` method, which will return
        the output of the delay process

    new_structure: list
        list of element construction dictionaries for the builder to assemble

    """
    import_modules['functions'].add("Delay")

    new_structure = []

    if len(subs) == 0:
        stateful_py_expr = 'Delay(lambda: %s, lambda: %s,'\
                           'lambda: %s, lambda: %s)' % (
                               delay_input, delay_time, initial_value, order)

    else:
        stateful_py_expr = 'Delay(lambda: _delinput_%s(),'\
                           'lambda: _deltime_%s(), lambda: _init_%s(),'\
                           'lambda: %s)' % (
                               identifier, identifier, identifier, order)

        # following elements not specified in the model file, but must exist
        # create the delay initialization element
        new_structure.append({
            'py_name': '_init_%s' % identifier,
            'real_name': 'Implicit',
            'kind': 'setup',  # not specified in the model file, but must exist
            'py_expr': initial_value,
            'subs': subs,
            'doc': 'Provides initial conditions for %s function' % identifier,
            'unit': 'See docs for %s' % identifier,
            'lims': 'None',
            'eqn': 'None',
            'arguments': ''
        })

        new_structure.append({
            'py_name': '_deltime_%s' % identifier,
            'real_name': 'Implicit',
            'kind': 'component',
            'doc': 'Provides delay time for %s function' % identifier,
            'subs': subs,
            'unit': 'See docs for %s' % identifier,
            'lims': 'None',
            'eqn': 'None',
            'py_expr': delay_time,
            'arguments': ''
        })

        new_structure.append({
            'py_name': '_delinput_%s' % identifier,
            'real_name': 'Implicit',
            'kind': 'component',
            'doc': 'Provides input for %s function' % identifier,
            'subs': subs,
            'unit': 'See docs for %s' % identifier,
            'lims': 'None',
            'eqn': 'None',
            'py_expr': delay_input,
            'arguments': ''
        })

    # describe the stateful object
    stateful = {
        'py_name': '_delay_%s' % identifier,
        'real_name': 'Delay of %s' % delay_input,
        'doc': 'Delay time: %s \n Delay initial value %s \n Delay order %s' % (
            delay_time, initial_value, order),
        'py_expr': stateful_py_expr,
        'unit': 'None',
        'lims': 'None',
        'eqn': 'None',
        'subs': '',
        'kind': 'stateful',
        'arguments': ''
    }
    new_structure.append(stateful)

    return "%s()" % stateful['py_name'], new_structure


def add_n_smooth(identifier, smooth_input, smooth_time, initial_value, order,
                 subs, subscript_dict):
    """
    Constructs stock and flow chains that implement the calculation of
    a smoothing function.

    Parameters
    ----------
    identifier: basestring
        the python-safe name of the stock

    smooth_input: <string>
        Reference to the model component that is the input to the
        smoothing function

    smooth_time: <string>
        Can be a number (in string format) or a reference to another model
        element which will calculate the delay. This is calculated throughout
        the simulation at runtime.

    initial_value: <string>
        This is used to initialize the stocks that are present in the delay.
        We initialize the stocks with equal values so that the outflow in
        the first timestep is equal to this value.

    order: string
        The number of stocks in the delay pipeline. As we construct the delays
        at build time, this must be an integer and cannot be calculated from
        other model components. Anything else will yield a ValueError.

    subs: list of strings
        List of strings of subscript indices that correspond to the
        list of expressions, and collectively define the shape of the output

    subscript_dict: dictionary
        Dictionary describing the possible dimensions of the stock's subscripts

    Returns
    -------
    reference: basestring
        reference to the smooth object `__call__` method, which will return
        the output of the smooth process

    new_structure: list
        list of element construction dictionaries for the builder to assemble

    """
    import_modules['functions'].add("Smooth")

    stateful = {
        'py_name': '_smooth_%s' % identifier,
        'real_name': 'Smooth of %s' % smooth_input,
        'doc': 'Smooth time: %s \n Smooth initial value %s \n Smooth order %s' % (
            smooth_time, initial_value, order),
        'py_expr': 'Smooth(lambda: %s, lambda: %s, lambda: %s, lambda: %s)' % (
            smooth_input, smooth_time, initial_value, order),
        'unit': 'None',
        'lims': 'None',
        'eqn': 'None',
        'subs': '',
        'kind': 'stateful',
        'arguments': ''
    }

    return "%s()" % stateful['py_name'], [stateful]


def add_n_trend(identifier, trend_input, average_time, initial_trend,
                subs, subscript_dict):
    """
    Trend.

    Parameters
    ----------
    identifier: basestring
        the python-safe name of the stock

    trend_input: <string>

    average_time: <string>


    trend_initial: <string>

    subs: list of strings
        List of strings of subscript indices that correspond to the
        list of expressions, and collectively define the shape of the output

    subscript_dict: dictionary
        Dictionary describing the possible dimensions of the stock's subscripts

    Returns
    -------
    reference: basestring
        reference to the trend object `__call__` method, which will return the
        output of the trend process

    new_structure: list
        list of element construction dictionaries for the builder to assemble

    """

    import_modules['functions'].add("Trend")

    stateful = {
        'py_name': '_trend_%s' % identifier,
        'real_name': 'trend of %s' % trend_input,
        'doc': 'Trend average time: %s \n Trend initial value %s' % (
            average_time, initial_trend),
        'py_expr': 'Trend(lambda: %s, lambda: %s, lambda: %s)' % (
            trend_input, average_time, initial_trend),
        'unit': 'None',
        'lims': 'None',
        'eqn': 'None',
        'subs': '',
        'kind': 'stateful',
        'arguments': ''
    }

    return "%s()" % stateful['py_name'], [stateful]


def add_initial(initial_input):
    """
    Constructs a stateful object for handling vensim's 'Initial' functionality

    Parameters
    ----------
    initial_input: basestring
        The expression which will be evaluated, and the first value of
        which returned

    Returns
    -------
    reference: basestring
        reference to the Initial object `__call__` method,
        which will return the first calculated value of `initial_input`

    new_structure: list
        list of element construction dictionaries for the builder to assemble

    """

    import_modules['functions'].add("Initial")

    stateful = {
        'py_name': utils.make_python_identifier('_initial_%s'
                                                % initial_input)[0],
        'real_name': 'Smooth of %s' % initial_input,
        'doc': 'Returns the value taken on during the initialization phase',
        'py_expr': 'Initial(lambda: %s)' % (
            initial_input),
        'unit': 'None',
        'lims': 'None',
        'eqn': 'None',
        'subs': '',
        'kind': 'stateful',
        'arguments': ''
    }

    return "%s()" % stateful['py_name'], [stateful]


def add_ext_data(identifier, file_name, tab, time_row_or_col, cell,
                 subs, subscript_dict, keyword):
    """
    Constructs a external object for handling Vensim's GET XLS DATA and
    GET DIRECT DATA functionality

    Parameters
    ----------
    identifier: basestring
        the python-safe name of the external values
    file_name: str
        filepath to the data
    tab: str
        tab where the data is
    time_row_or_col: str
        identifier to the starting point of the time dimension
    cell: str
        cell identifier where the data starts
    subs: list of strings
        List of strings of subscript indices that correspond to the
        list of expressions, and collectively define the shape of the output
    subscript_dict: dictionary
        Dictionary describing the possible dimensions of the stock's subscripts
    keyword: str
        Data retrieval method ('interpolate', 'look forward', 'hold backward')

    Returns
    -------
    reference: basestring
        reference to the ExtData object `__call__` method,
        which will return the retrieved value of data for the current time step
    new_structure: list
        list of element construction dictionaries for the builder to assemble

    """
    coords = utils.make_coord_dict(subs, subscript_dict, terse=False)
    keyword = "'%s'" % keyword.strip(':').lower()\
              if isinstance(keyword, str) else keyword
    name = utils.make_python_identifier('_ext_data_%s' % identifier)[0]

    import_modules['external'].add("ExtData")

    # Check if the object already exists
    if name in build_names:
        # Create a new py_name with ADD_# ending
        # This object name will not be used in the model as
        # the information is added to the existing object
        # with add method.
        kind = 'external_add'
        name = utils.make_add_identifier(name, build_names)
        py_expr = '.add(%s, %s, %s, %s, %s, %s)'
    else:
        # Regular name will be used and a new object will be created
        # in the model file.
        build_names.add(name)
        kind = 'external'
        py_expr = 'ExtData(%s, %s, %s, %s, %s, %s,'\
                  '        _root, \'{}\')'.format(name)

    external = {
        'py_name': name,
        'real_name': 'External data for %s' % identifier,
        'doc': 'Provides data for data variable %s' % identifier,
        'py_expr': py_expr % (file_name, tab, time_row_or_col,
                              cell, keyword, coords),
        'unit': 'None',
        'lims': 'None',
        'eqn': 'None',
        'subs': subs,
        'kind': kind,
        'arguments': ''
    }

    return "%s(time())" % external['py_name'], [external]


def add_ext_constant(identifier, file_name, tab, cell,
                     subs, subscript_dict):
    """
    Constructs a external object for handling Vensim's GET XLS CONSTANT and
    GET DIRECT CONSTANT functionality

    Parameters
    ----------
    identifier: basestring
        the python-safe name of the external values
    file_name: str
        filepath to the data
    tab: str
        tab where the data is
    cell: str
        cell identifier where the data starts
    subs: list of strings
        List of strings of subscript indices that correspond to the
        list of expressions, and collectively define the shape of the output
    subscript_dict: dictionary
        Dictionary describing the possible dimensions of the stock's subscripts

    Returns
    -------
    reference: basestring
        reference to the ExtConstant object `__call__` method,
        which will return the read value of the data
    new_structure: list
        list of element construction dictionaries for the builder to assemble

    """
    import_modules['external'].add("ExtConstant")

    coords = utils.make_coord_dict(subs, subscript_dict, terse=False)
    name = utils.make_python_identifier('_ext_constant_%s' % identifier)[0]

    # Check if the object already exists
    if name in build_names:
        # Create a new py_name with ADD_# ending
        # This object name will not be used in the model as
        # the information is added to the existing object
        # with add method.
        kind = 'external_add'
        name = utils.make_add_identifier(name, build_names)
        py_expr = '.add(%s, %s, %s, %s)'
    else:
        # Regular name will be used and a new object will be created
        # in the model file.
        kind = 'external'
        py_expr = 'ExtConstant(%s, %s, %s, %s,'\
                  '            _root, \'{}\')'.format(name)
    build_names.add(name)

    external = {
        'py_name': name,
        'real_name': 'External constant for %s' % identifier,
        'doc': 'Provides data for constant data variable %s' % identifier,
        'py_expr': py_expr % (file_name, tab, cell, coords),
        'unit': 'None',
        'lims': 'None',
        'eqn': 'None',
        'subs': subs,
        'kind': kind,
        'arguments': ''
    }

    return "%s()" % external['py_name'], [external]


def add_ext_lookup(identifier, file_name, tab, x_row_or_col, cell,
                   subs, subscript_dict):
    """
    Constructs a external object for handling Vensim's GET XLS LOOKUPS and
    GET DIRECT LOOKUPS functionality

    Parameters
    ----------
    identifier: basestring
        the python-safe name of the external values
    file_name: str
        filepath to the data
    tab: str
        tab where the data is
    x_row_or_col: str
        identifier to the starting point of the lookup dimension
    cell: str
        cell identifier where the data starts
    subs: list of strings
        List of strings of subscript indices that correspond to the
        list of expressions, and collectively define the shape of the output
    subscript_dict: dictionary
        Dictionary describing the possible dimensions of the stock's subscripts

    Returns
    -------
    reference: basestring
        reference to the ExtLookup object `__call__` method,
        which will return the retrieved value of data after interpolating it
    new_structure: list
        list of element construction dictionaries for the builder to assemble

    """
    import_modules['external'].add("ExtLookup")

    coords = utils.make_coord_dict(subs, subscript_dict, terse=False)
    name = utils.make_python_identifier('_ext_lookup_%s' % identifier)[0]

    # Check if the object already exists
    if name in build_names:
        # Create a new py_name with ADD_# ending
        # This object name will not be used in the model as
        # the information is added to the existing object
        # with add method.
        kind = 'external_add'
        name = utils.make_add_identifier(name, build_names)
        py_expr = '.add(%s, %s, %s, %s, %s)'
    else:
        # Regular name will be used and a new object will be created
        # in the model file.
        kind = 'external'
        py_expr = 'ExtLookup(%s, %s, %s, %s, %s,\n'\
                  '          _root, \'{}\')'.format(name)
    build_names.add(name)

    external = {
        'py_name': name,
        'real_name': 'External lookup data for %s' % identifier,
        'doc': 'Provides data for external lookup variable %s' % identifier,
        'py_expr': py_expr % (file_name, tab, x_row_or_col, cell, coords),
        'unit': 'None',
        'lims': 'None',
        'eqn': 'None',
        'subs': subs,
        'kind': kind,
        'arguments': 'x'
    }

    return "%s(x)" % external['py_name'], [external]


def add_macro(macro_name, filename, arg_names, arg_vals):
    """
    Constructs a stateful object instantiating a 'Macro'

    Parameters
    ----------
    macro_name: basestring
        python safe name for macro
    filename: basestring
        filepath to macro definition
    func_args: dict
        dictionary of values to be passed to macro
        {key: function}

    Returns
    -------
    reference: basestring
        reference to the Initial object `__call__` method,
        which will return the first calculated value of `initial_input`
    new_structure: list
        list of element construction dictionaries for the builder to assemble

    """
    import_modules['functions'].add("Macro")

    func_args = '{ %s }' % ', '.join(["'%s': lambda: %s" % (key, val)
                                      for key, val in
                                      zip(arg_names, arg_vals)])

    stateful = {
        'py_name': '_macro_' + macro_name + '_' + '_'.join(
            [utils.make_python_identifier(f)[0] for f in arg_vals]),
        'real_name': 'Macro Instantiation of ' + macro_name,
        'doc': 'Instantiates the Macro',
        'py_expr': "Macro('%s', %s, '%s',"
                   "time_initialization=lambda: __data['time'])" % (
                   filename, func_args, macro_name),
        'unit': 'None',
        'lims': 'None',
        'eqn': 'None',
        'subs': '',
        'kind': 'stateful',
        'arguments': ''
    }

    return "%s()" % stateful['py_name'], [stateful]


def add_incomplete(var_name, dependencies):
    """
    Incomplete functions don't really need to be 'builders' as they
     add no new real structure, but it's helpful to have a function
     in which we can raise a warning about the incomplete equation
     at translate time.
    """
    import_modules['functions'].add("incomplete")

    warnings.warn('%s has no equation specified' % var_name,
                  SyntaxWarning, stacklevel=2)

    # first arg is `self` reference
    return "incomplete(%s)" % ', '.join(dependencies), []


def build_function_call(function_def, user_arguments):
    """

    Parameters
    ----------
    function_def: function definition map with following keys
        - name: name of the function
        - parameters: list with description of all parameters of this function
            - name
            - optional?
            - type: [
                "expression", - provide converted expression as parameter for
                                runtime evaluating before the method call
                "lambda",     - provide lambda expression as parameter for
                                delayed runtime evaluation in the method call
                "time",       - provide access to current instance of
                                time object
                "scope"       - provide access to current instance of
                                scope object (instance of Macro object)
            ]
    user_arguments: list of arguments provided from model

    Returns
    -------

    """
    if isinstance(function_def, str):
        return function_def + "(" + ",".join(user_arguments) + ")"

    if function_def["name"] == "not_implemented_function":
        user_arguments = ["'" + function_def["original_name"] + "'"]\
                         + user_arguments
        warnings.warn(
            "\n\nTrying to translate " + function_def["original_name"]
            + " which it is not implemented on PySD. The translated "
            + "model will crash... ")

    if "module" in function_def:
        if function_def["module"] in ["numpy", "xarray"]:
            # import external modules
            import_modules[function_def['module']] = True
        else:
            # import method from PySD module
            import_modules[function_def['module']].add(function_def['name'])

    if "parameters" in function_def:
        parameters = function_def["parameters"]
        arguments = []
        argument_idx = 0
        for parameter_idx in range(len(parameters)):
            parameter_def = parameters[parameter_idx]
            is_optional = parameter_def["optional"]\
                if "optional" in parameter_def else False
            if argument_idx >= len(user_arguments) and is_optional:
                break

            parameter_type = parameter_def["type"]\
                if "type" in parameter_def else "expression"

            user_argument = user_arguments[argument_idx]
            if parameter_type in ["expression", "lambda"]:
                argument_idx += 1

            arguments.append({
                                 "expression": user_argument,
                                 "lambda": "lambda: " + user_argument,
                                 "time": "__data['time']",
                                 "scope": "__data['scope']"
                             }[parameter_type])

        return function_def['name'] + "(" + ", ".join(arguments) + ")"

    return function_def['name'] + "(" + ",".join(user_arguments) + ")"

import datetime
import httplib
import json
import re
import requests
from . import models
from . import utils
from .exceptions import (OnfleetError, OnfleetResourceNotFound,
    OnfleetRatelimitExceeded, MultipleDestinationsError)

# This regex takes the returned onfleet string and gets the list that onfleet
# returns as a string literal, i.e.
# "['1252 Howard St, San Francisco, CA', '73 Sumner St, San Francisco, CA']"
options_re = re.compile(r'Options = (\[.*\])')


def parse_options(cause):
    """Parse the onfleet string for the proposed options."""
    match = options_re.search(cause)
    if match:
        # Onfleet returns a string that contains a list containing strings,
        # which should be valid JSON. Let's return the options as an actual
        # python list
        options_list_as_string = match.group(1)
        try:
            return json.loads(options_list_as_string)
        except ValueError:
            # Bad JSON, let this fall to return None
            pass
    return None


ONFLEET_API_ENDPOINT = "https://onfleet.com/api/v2/"


class ComplexEncoder(json.JSONEncoder):
    def default(self, obj):
        payload = None

        if isinstance(obj, models.Administrator):
            payload = {
                'name': obj.name,
                'email': obj.email
            }

            optional_properties = {
                'phone': 'phone',
            }
        elif isinstance(obj, models.Vehicle):
            payload = {'type': obj.vehicle_type}

            optional_properties = {
                'description': 'description',
                'license_plate': 'licensePlate',
                'color': 'color'
            }
        elif isinstance(obj, models.Worker):
            payload = {
            }

            optional_properties = {
                'vehicle': 'vehicle',
                'tasks': 'tasks',
                'name': 'name',
                'phone': 'phone',
                'team_ids': 'teams'
            }
        elif isinstance(obj, models.Address):
            payload = {
            }

            optional_properties = {
                'street': 'street',
                'number': 'number',
                'city': 'city',
                'country': 'country',
                'name': 'name',
                'apartment': 'apartment',
                'state': 'state',
                'postal_code': 'postalCode',
                'unparsed': 'unparsed',
            }
        elif isinstance(obj, models.Destination):
            payload = {
            }

            optional_properties = {
                'address': 'address',
                'location': 'location',
                'notes': 'notes',
            }
        elif isinstance(obj, models.Task):
            payload = {
                'merchant': obj.merchant,
                'executor': obj.executor,
                'destination': obj.destination,
                'recipients': obj.recipients,
            }

            optional_properties = {
                'notes': 'notes',
                'pickup_task': 'pickupTask',
                'dependencies': 'dependencies',
                'complete_after': 'completeAfter',
                'complete_before': 'completeBefore',
                'container': 'container',
            }
        elif isinstance(obj, models.Recipient):
            payload = {
                'name': obj.name,
                'phone': obj.phone,
                'notes': obj.notes,
            }

            optional_properties = {}
        elif isinstance(obj, models.Administrator):
            payload = {
                'name': obj.name,
                'email': obj.email,
            }

            optional_properties = {'phone': obj.phone}
        if payload is None:
            return json.JSONEncoder.default(self, obj)
        else:
            for key, value in optional_properties.iteritems():
                if hasattr(obj, key) and getattr(obj, key) is not None:
                    if isinstance(getattr(obj, key), datetime.datetime):
                        payload[value] = utils.to_unix_time(getattr(obj, key))
                    else:
                        payload[value] = getattr(obj, key)

            return payload


class Onfleet(object):
    def __init__(self, api_key):
        self.api_key = api_key

    def __getattr__(self, k):
        return OnfleetCall(self.api_key, k)


class OnfleetCall(object):
    def __init__(self, api_key, path):
        self.api_key = api_key
        self.components = [path]

    def __getattr__(self, k):
        self.components.append(k)
        return self

    def __getitem__(self, k):
        if not k:
            raise KeyError("You can't retrieve a falsy object!")
        self.components.append(k)
        return self

    def __call__(self, *args, **kwargs):
        url = "{}{}".format(ONFLEET_API_ENDPOINT, "/".join(self.components))
        if 'method' in kwargs:
            method = kwargs['method']
            del kwargs['method']
        else:
            method = "GET"

        parse_response = kwargs.get('parse_response', True)

        if 'parse_response' in kwargs:
            del kwargs['parse_response']

        fun = getattr(requests, method.lower())
        if len(args) > 0:
            data = ComplexEncoder().encode(args[0])
        else:
            data = None
        response = fun(url, data=data, params=kwargs, auth=(self.api_key, ''), verify=False)

        parse_dictionary = {
            'workers': models.Worker,
            'tasks': models.Task,
            'recipients': models.Recipient,
            'destinations': models.Destination,
            'organization': models.Organization,
            'admins': models.Administrator
        }

        parse_as = None
        component_name = ''
        for component, parser in parse_dictionary.iteritems():
            if component in self.components:
                parse_as = parser
                component_name = component

        if response.text:
            # If the response is not falsy.

            json_response = response.json()

            if 'code' in json_response:
                # So presumably this is an error response,
                # Map these to better python variable names, since onfleet uses
                # horrible naming conventions
                error_type = json_response['code']
                error_data = json_response['message']

                # Onfleet provides virtually zero consistency with the format
                # returned by their API. In some cases, error_data is a dict
                # with more information; in other cases, it is just a string.
                if isinstance(error_data, dict):
                    error_cause = error_data['cause']
                    error_code = error_data['error']
                    error_message = error_data['message']
                elif response.status_code == 429:
                    raise OnfleetRatelimitExceeded(
                        error_data,
                        error_type,
                        None,
                        None,
                    )

                # error_cause is a string when there is a geocoding error,
                # BUT it's a dict in many other cases.... We only want to try
                # to parse options if it's a string
                if isinstance(error_cause, basestring):
                    options = parse_options(error_cause)

                    if options:
                        # If the options regex returned some options, this must be
                        # a multiple destination issue. This is the only way to
                        # tell since onfleet doesn't use distinct error codes
                        raise MultipleDestinationsError(
                            options,
                            error_message,
                            error_type,
                            error_code,
                            error_cause,
                        )

                if response.status_code == httplib.NOT_FOUND:
                    raise OnfleetResourceNotFound(
                        error_message,
                        error_type,
                        error_code,
                        error_cause,
                    )

                raise OnfleetError(error_message, error_type, error_code,
                    error_cause)

            if parse_response and parse_as is not None:
                if isinstance(json_response, list):
                    return map(parse_as.parse, json_response)
                # Handle pagination case, where Onfleet supplies lastId.
                elif 'lastId' in json_response:
                    return {
                        'lastId': json_response['lastId'],
                        'results': map(parse_as.parse,
                            json_response[component_name]),
                    }
                else:
                    return parse_as.parse(json_response)
            else:
                return json_response
        return None

import argparse
import json
import logging
import os
import requests
import sys
import urllib3

urllib3.disable_warnings()


HEADERS = {'kbn-xsrf': 'true'}
AUTH = None  # requests (user, password) tuple, set from CLI args / env in __main__

level = os.getenv('LOG_LEVEL', 'DEBUG')
logger = logging.getLogger(__name__)
logger.setLevel(level)
logHandler = logging.StreamHandler(sys.stdout)
logger.addHandler(logHandler)


def get_index_patterns(url):
    '''
    Returns the existing saved index pattern objects
    '''
    existing_patterns_resp = requests.get(
        url + '/api/saved_objects/_find?fields=title&fields=type&per_page=10000&type=index-pattern',
        headers=HEADERS,
        auth=AUTH,
        verify=False,
        timeout=30)

    if existing_patterns_resp.status_code == 200:
        existing_patterns_data = existing_patterns_resp.json()
        return existing_patterns_data['saved_objects']

    logger.error('Failed to get index patterns from kibana with status: %s %s',
        existing_patterns_resp.status_code, existing_patterns_resp.text)
    sys.exit(1)


def create_index_pattern(url, pattern):
    '''
    Create pattern in kibana
    '''

    logger.info('Creating index pattern: %s', pattern)

    payload = {
        "attributes": {
            "title": pattern + "-*",
            "timeFieldName": "@timestamp"
        }
    }

    index_create_resp = requests.post(
        url + '/api/saved_objects/index-pattern/' + pattern,
        json=payload,
        headers=HEADERS,
        auth=AUTH,
        verify=False,
        timeout=30)

    if index_create_resp.status_code != 200:
        logger.error('Failed to post index pattern: %s to Kibana with status: %s %s',
            pattern, index_create_resp.status_code, index_create_resp.text)
        sys.exit(1)

    logger.info('Success')


def refresh_field_list(url, index_pattern):
    '''
    Given a list of (all) saved index pattern objects, update the field lists on them
    '''

    pattern = index_pattern + "-*"
    pattern_id = index_pattern

    logger.info('Getting fields for "%s" index pattern', pattern)

    # NOTE: /api/index_patterns/_fields_for_wildcard (pre-8.x path) now 404s.
    # Kibana moved this to the internal Data Views API, which additionally
    # requires the x-elastic-internal-origin header or it also 404s.
    fields_headers = dict(HEADERS)
    fields_headers['x-elastic-internal-origin'] = 'kibana'
    index_fields_resp = requests.get(
        url +
            '/internal/data_views/_fields_for_wildcard?pattern=' +
            pattern +
            '&meta_fields=_source&meta_fields=_id&meta_fields=_type&meta_fields=_index&meta_fields=_score',
        headers=fields_headers,
        auth=AUTH,
        verify=False,
        timeout=30)

    if index_fields_resp.status_code == 200:
        index_fields_data = index_fields_resp.json()
    else:
        logger.error('Failed to get field list from kibana for pattern: %s', pattern)
        sys.exit(1)

    payload = {
        'attributes': {
            'title': pattern,
            'timeFieldName': '@timestamp',
            'fields': json.dumps(index_fields_data['fields'])
        }
    }

    logger.info('Putting new field mappings for pattern: %s with id: %s', pattern, pattern_id)

    pattern_update_resp = requests.put(
        url + '/api/saved_objects/index-pattern/' + pattern_id,
        json=payload,
        headers=HEADERS,
        auth=AUTH,
        verify=False,
        timeout=30)

    if pattern_update_resp.status_code != 200:
        logger.error('Failed to put field list to kibana for pattern: %s', pattern)
        sys.exit(1)

    logger.info('Success')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Index pattern manage')
    parser.add_argument("--url", dest="KIBANA_URL", required=True, help='Kibana URL. Example: http://127.0.0.1:5601')
    parser.add_argument("-i", dest="INDEX_PATTERN_ID", required=True, help='Index pattern ID. Example: cyberpolygon')
    parser.add_argument("--api-key", default=os.environ.get("ES_API_KEY"),
                         help="Encoded API key (id:api_key, base64) - sent as 'Authorization: ApiKey ...'")
    parser.add_argument("--user", default=os.environ.get("ES_USER", "elastic"))
    parser.add_argument("--password", default=os.environ.get("ES_PASSWORD") or os.environ.get("ELASTIC_PASSWORD"))

    args = parser.parse_args()

    if args.api_key:
        HEADERS['Authorization'] = f'ApiKey {args.api_key}'
    elif args.password:
        AUTH = (args.user, args.password)

    index_patterns = get_index_patterns(args.KIBANA_URL)
    logger.info('Check index patterns')
    for i_p in index_patterns:
        if i_p['id'] == args.INDEX_PATTERN_ID:
            logger.info('index pattern "%s" already exists', args.INDEX_PATTERN_ID)
            refresh_field_list(args.KIBANA_URL, args.INDEX_PATTERN_ID)
            sys.exit(0)
    logger.info('index pattern "%s" not found', args.INDEX_PATTERN_ID)
    create_index_pattern(args.KIBANA_URL, args.INDEX_PATTERN_ID)
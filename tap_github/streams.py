from datetime import datetime, timedelta
import singer
from singer import (metrics, bookmarks, metadata)

LOGGER = singer.get_logger()
DATE_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
PER_PAGE_NUMBER = 100
DATE_RANGE_WINDOW = 7

def get_bookmark(state, repo, stream_name, bookmark_key, start_date, is_incremental = True):
    """
    Return bookmark value if available in the state otherwise return start date
    """
    if is_incremental:
        repo_stream_dict = bookmarks.get_bookmark(state, repo, stream_name)
        if repo_stream_dict:
            return repo_stream_dict.get(bookmark_key)
    return start_date

def get_date_ranges(start_date, end_date, date_range_window=DATE_RANGE_WINDOW):
    """
    Return a list of date ranges to be used for the API calls.
    """
    start_date = datetime.strptime(start_date, DATE_FORMAT)
    end_date = datetime.strptime(end_date, DATE_FORMAT)
    while start_date < end_date:
        temp_end_date=start_date + timedelta(days=date_range_window)
        date_ranges=(start_date.strftime(DATE_FORMAT),temp_end_date.strftime(DATE_FORMAT))
        start_date = temp_end_date
        yield date_ranges

def get_schema(catalog, stream_id):
    """
    Return catalog of the specified stream.
    """
    stream_catalog = [cat for cat in catalog if cat['tap_stream_id'] == stream_id ][0]
    return stream_catalog

def get_child_full_url(domain, child_object, repo_path, parent_id, grand_parent_id):
    """
    Build the child stream's URL based on the parent and the grandparent's ids.
    """

    if child_object.no_path:
        return
    elif child_object.use_repository:
        # The `use_repository` represents that the url contains /repos and the repository name.
        child_full_url = '{}/repos/{}/{}'.format(
            domain,
            repo_path,
            child_object.path).format(*parent_id)

    elif child_object.use_organization:
        # The `use_organization` represents that the url contains the organization name.
        child_full_url = '{}/{}'.format(
            domain,
            child_object.path).format(repo_path, *parent_id, *grand_parent_id)

    else:
        # Build and return url that does not contain the repos or the organization name.
        # Example: https://base_url/projects/{project_id}/columns
        child_full_url = '{}/{}'.format(
            domain,
            child_object.path).format(*grand_parent_id)
    LOGGER.info("Final url is: %s", child_full_url)

    return child_full_url


class Stream:
    """
    A base class representing tap-github streams.
    """
    tap_stream_id = None
    replication_method = None
    replication_keys = None
    key_properties = []
    path = None
    since_filter_param = ""
    since_filter_param_custom = ""
    additional_filters = ""
    id_keys = []
    use_organization = False
    children = []
    pk_child_fields = []
    use_repository = False
    headers = {'Accept': '*/*'}
    parent = None
    inherit_parent_fields = []
    inherit_array_parent_fields = ""
    custom_column_name = ""
    no_path = False
    result_path = ""

    def build_url(self, base_url, repo_path, bookmark):
        """
        Build the full url with parameters and attributes.
        """
        if self.since_filter_param:
            # Add the since parameter for incremental streams
            query_string = '?since={}{}'.format(bookmark,self.since_filter_param)
        elif self.since_filter_param_custom:
            # Add additional custom filter for incremental streams
            query_string = f'?{self.since_filter_param_custom}'.format(**bookmark)
        elif self.additional_filters:
            query_string = f'?{self.additional_filters}'
        else:
            query_string = ''

        if self.use_organization:
            # The `use_organization` represents that the url contains the organization name.
            full_url = '{}/{}'.format(
                base_url,
                self.path).format(repo_path)
        else:
            # The url that contains /repos and the repository name.
            full_url = '{}/repos/{}/{}{}'.format(
                base_url,
                repo_path,
                self.path,
                query_string)

        LOGGER.info("Final url is: %s", full_url)
        return full_url

    def get_min_bookmark(self, stream, selected_streams, bookmark, repo_path, start_date, state):
        """
        Get the minimum bookmark from the parent and its corresponding child bookmarks.
        """

        stream_obj = STREAMS[stream]()
        min_bookmark = bookmark
        if stream in selected_streams:
            # Get minimum of stream's bookmark(start date in case of no bookmark) and min_bookmark
            min_bookmark = min(min_bookmark, get_bookmark(state, repo_path, stream, "since", start_date))
            LOGGER.debug("New minimum bookmark is %s", min_bookmark)

        for child in stream_obj.children:
            # Iterate through all children and return minimum bookmark among all.
            min_bookmark = min(min_bookmark, self.get_min_bookmark(child, selected_streams, min_bookmark, repo_path, start_date, state))

        return min_bookmark

    def write_bookmarks(self, stream, selected_streams, bookmark_value, repo_path, state):
        """Write the bookmark in the state corresponding to the stream."""
        stream_obj = STREAMS[stream]()

        # If the stream is selected, write the bookmark.
        if stream in selected_streams:
            singer.write_bookmark(state, repo_path, stream_obj.tap_stream_id, {"since": bookmark_value})

        # For the each child, write the bookmark if it is selected.
        for child in stream_obj.children:
            self.write_bookmarks(child, selected_streams, bookmark_value, repo_path, state)

    # pylint: disable=no-self-use
    def get_child_records(self,
                          client,
                          catalog,
                          child_stream,
                          grand_parent_id,
                          repo_path,
                          state,
                          start_date,
                          bookmark_dttm,
                          stream_to_sync,
                          selected_stream_ids,
                          parent_id = None,
                          parent_record = None):
        """
        Retrieve and write all the child records for each updated parent based on the parent record and its ids.
        """
        child_object = STREAMS[child_stream]()

        is_stream_incremental = child_object.replication_method == "INCREMENTAL" and child_object.replication_keys
        child_bookmark_value = get_bookmark(state, repo_path, child_object.tap_stream_id, "since", start_date, is_stream_incremental)

        if not parent_id:
            parent_id = grand_parent_id

        child_full_url = get_child_full_url(client.base_url, child_object, repo_path, parent_id, grand_parent_id)
        stream_catalog = get_schema(catalog, child_object.tap_stream_id)
        with metrics.record_counter(child_object.tap_stream_id) as counter:
            if child_full_url is not None:
                for response in client.authed_get_all_pages(
                    child_object.tap_stream_id,
                    child_full_url,
                    stream = child_object.tap_stream_id
                ):
                    records = response.json()
                    if child_object.result_path: records = records.get(child_object.result_path,[])
                    extraction_time = singer.utils.now()

                    if isinstance(records, list):
                        # Loop through all the records of response
                        for record in records:
                            record['_sdc_repository'] = repo_path
                            for column, field in child_object.inherit_parent_fields:
                                record[column] = parent_record.get(field)
                            child_object.add_fields_at_1st_level(record = record, parent_record = parent_record)

                            with singer.Transformer() as transformer:

                                rec = transformer.transform(record, stream_catalog['schema'], metadata=metadata.to_map(stream_catalog['metadata']))

                                if child_object.tap_stream_id in selected_stream_ids and record.get(child_object.replication_keys, start_date) >= child_bookmark_value:
                                    singer.write_record(child_object.tap_stream_id, rec, time_extracted=extraction_time)
                                    counter.increment()

                            # Loop thru each child and nested child in the parent and fetch all the child records.
                            for nested_child in child_object.children:
                                if nested_child in stream_to_sync:
                                    # Collect id of child record to pass in the API of its sub-child.
                                    child_id = tuple(record.get(key) for key in STREAMS[nested_child]().id_keys)
                                    # Here, grand_parent_id is the id of 1st level parent(main parent) which is required to
                                    # pass in the API of the current child's sub-child.
                                    child_object.get_child_records(client, catalog, nested_child, child_id, repo_path, state, start_date, bookmark_dttm, stream_to_sync, selected_stream_ids, grand_parent_id, record)

                    else:
                        # Write JSON response directly if it is a single record only.
                        records['_sdc_repository'] = repo_path
                        for column, field in child_object.inherit_parent_fields:
                            records[column] = parent_record.get(field)
                        child_object.add_fields_at_1st_level(record = records, parent_record = parent_record)

                        with singer.Transformer() as transformer:

                            rec = transformer.transform(records, stream_catalog['schema'], metadata=metadata.to_map(stream_catalog['metadata']))
                            if child_object.tap_stream_id in selected_stream_ids and records.get(child_object.replication_keys, start_date) >= child_bookmark_value :

                                singer.write_record(child_object.tap_stream_id, rec, time_extracted=extraction_time)
            elif child_object.no_path:
                records = []
                extraction_time = singer.utils.now()
                if child_object.inherit_array_parent_fields: 
                    for record in parent_record.get(child_object.inherit_array_parent_fields,[]):
                        if col_name := child_object.custom_column_name: 
                            records.append({col_name: record})
                        else:
                            records.append(record)
                else: records.append({})
                for record in records:
                    for column, field in child_object.inherit_parent_fields:
                        record[column] = parent_record.get(field)
                    child_object.add_fields_at_1st_level(record = record, parent_record = parent_record)
                    with singer.Transformer() as transformer:

                        rec = transformer.transform(record, stream_catalog['schema'], metadata=metadata.to_map(stream_catalog['metadata']))

                        if child_object.tap_stream_id in selected_stream_ids:
                            singer.write_record(child_object.tap_stream_id, rec, time_extracted=extraction_time)
                            counter.increment()

                    # Loop thru each child and nested child in the parent and fetch all the child records.
                    for nested_child in child_object.children:
                        if nested_child in stream_to_sync:
                            # Collect id of child record to pass in the API of its sub-child.
                            child_id = tuple(record.get(key) for key in STREAMS[nested_child]().id_keys)
                            if STREAMS[nested_child]().id_keys and not all(child_id): continue
                            # Here, grand_parent_id is the id of 1st level parent(main parent) which is required to
                            # pass in the API of the current child's sub-child.
                            child_object.get_child_records(client, catalog, nested_child, child_id, repo_path, state, start_date, bookmark_dttm, stream_to_sync, selected_stream_ids, grand_parent_id, record)

    # pylint: disable=unnecessary-pass
    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        pass
    
    def get_field(self,record, field_path):
        """
        Get a field of a record from a field path
        """
        response = record
        for path in field_path:
            response = response.get(path)
            if not response: return
        return response

class FullTableStream(Stream):
    def sync_endpoint(self,
                        client,
                        state,
                        catalog,
                        repo_path,
                        start_date,
                        selected_stream_ids,
                        stream_to_sync,
                        config,
                        ):
        """
        A common function sync full table streams.
        """

        # build full url
        full_url = self.build_url(client.base_url, repo_path, None)

        stream_catalog = get_schema(catalog, self.tap_stream_id)
        with metrics.record_counter(self.tap_stream_id) as counter:
            for response in client.authed_get_all_pages(
                    self.tap_stream_id,
                    full_url,
                    self.headers,
                    stream = self.tap_stream_id
            ):
                records = response.json()
                if self.result_path: records = records.get(self.result_path,[])
                extraction_time = singer.utils.now()
                # Loop through all records
                for record in records:

                    record['_sdc_repository'] = repo_path
                    self.add_fields_at_1st_level(record = record, parent_record = None)

                    with singer.Transformer() as transformer:
                        rec = transformer.transform(record, stream_catalog['schema'], metadata=metadata.to_map(stream_catalog['metadata']))
                        if self.tap_stream_id in selected_stream_ids:

                            singer.write_record(self.tap_stream_id, rec, time_extracted=extraction_time)

                            counter.increment()

                    for child in self.children:
                        if child in stream_to_sync:

                            parent_id = tuple(record.get(key) for key in STREAMS[child]().id_keys)
                            if STREAMS[child]().id_keys and not all(parent_id):
                                pass
                            else:
                                # Sync child stream, if it is selected or its nested child is selected.
                                self.get_child_records(client,
                                                    catalog,
                                                    child,
                                                    parent_id,
                                                    repo_path,
                                                    state,
                                                    start_date,
                                                    record.get(self.replication_keys),
                                                    stream_to_sync,
                                                    selected_stream_ids,
                                                    parent_record = record)
        return state

class IncrementalStream(Stream):
    def sync_endpoint(self,
                      client,
                      state,
                      catalog,
                      repo_path,
                      start_date,
                      selected_stream_ids,
                      stream_to_sync,
                      config,
                      ):

        """
        A common function sync incremental streams. Sync an incremental stream for which records are not
        in descending order. For, incremental streams iterate all records, write only newly updated records and
        write the latest bookmark value.
        """

        parent_bookmark_value = get_bookmark(state, repo_path, self.tap_stream_id, "since", start_date)
        current_time = datetime.today().strftime(DATE_FORMAT)
        min_bookmark_value = self.get_min_bookmark(self.tap_stream_id, selected_stream_ids, current_time, repo_path, start_date, state)

        max_bookmark_value = min_bookmark_value

        # build full url
        full_url = self.build_url(client.base_url, repo_path, min_bookmark_value)

        stream_catalog = get_schema(catalog, self.tap_stream_id)

        with metrics.record_counter(self.tap_stream_id) as counter:
            for response in client.authed_get_all_pages(
                    self.tap_stream_id,
                    full_url,
                    self.headers,
                    stream = self.tap_stream_id
            ):
                records = response.json()
                if self.result_path: records = records.get(self.result_path,[])
                extraction_time = singer.utils.now()
                # Loop through all records
                for record in records:
                    record['_sdc_repository'] = repo_path
                    self.add_fields_at_1st_level(record = record, parent_record = None)

                    with singer.Transformer() as transformer:
                        if record.get(self.replication_keys):
                            if record[self.replication_keys] >= max_bookmark_value:
                                # Update max_bookmark_value
                                max_bookmark_value = record[self.replication_keys]

                            bookmark_dttm = record[self.replication_keys]

                            # Keep only records whose bookmark is after the last_datetime
                            if bookmark_dttm >= min_bookmark_value:
                                if self.tap_stream_id in selected_stream_ids and bookmark_dttm >= parent_bookmark_value:
                                    rec = transformer.transform(record, stream_catalog['schema'], metadata=metadata.to_map(stream_catalog['metadata']))

                                    singer.write_record(self.tap_stream_id, rec, time_extracted=extraction_time)
                                    counter.increment()

                                for child in self.children:
                                    if child in stream_to_sync:

                                        parent_id = tuple(record.get(key) for key in STREAMS[child]().id_keys)
                                        if STREAMS[child]().id_keys and not all(parent_id):
                                            pass
                                        else:
                                            # Sync child stream, if it is selected or its nested child is selected.
                                            self.get_child_records(client,
                                                                catalog,
                                                                child,
                                                                parent_id,
                                                                repo_path,
                                                                state,
                                                                start_date,
                                                                record.get(self.replication_keys),
                                                                stream_to_sync,
                                                                selected_stream_ids,
                                                                parent_record = record)
                        else:
                            LOGGER.warning("Skipping this record for %s stream with %s = %s as it is missing replication key %s.",
                                        self.tap_stream_id, self.key_properties, record[self.key_properties], self.replication_keys)

            # Write bookmark for incremental stream.
            self.write_bookmarks(self.tap_stream_id, selected_stream_ids, max_bookmark_value, repo_path, state)

        return state
    
class IncrementalDateStream(Stream):
    def sync_endpoint(self,
                      client,
                      state,
                      catalog,
                      repo_path,
                      start_date,
                      selected_stream_ids,
                      stream_to_sync,
                      config,
                      ):

        """
        A common function sync incremental streams. Sync an incremental stream for which records are not
        in descending order. For, incremental streams iterate all records, write only newly updated records and
        write the latest bookmark value.
        """

        parent_bookmark_value = get_bookmark(state, repo_path, self.tap_stream_id, "since", start_date)
        current_time = datetime.today().strftime(DATE_FORMAT)
        min_bookmark_value = self.get_min_bookmark(self.tap_stream_id, selected_stream_ids, current_time, repo_path, start_date, state)

        max_bookmark_value = min_bookmark_value
        LOGGER.info(f'Starting stream with bookmark {min_bookmark_value} and current time {current_time}')
        for start_date, end_date in get_date_ranges(min_bookmark_value, current_time, config.get('date_range_window', DATE_RANGE_WINDOW)):
            # build full url
            full_url = self.build_url(client.base_url, repo_path, {'from': start_date, 'until': end_date})

            stream_catalog = get_schema(catalog, self.tap_stream_id)

            with metrics.record_counter(self.tap_stream_id) as counter:
                for response in client.authed_get_all_pages(
                        self.tap_stream_id,
                        full_url,
                        self.headers,
                        stream = self.tap_stream_id
                ):
                    records = response.json()
                    if self.result_path: records = records.get(self.result_path,[])
                    extraction_time = singer.utils.now()
                    # Loop through all records
                    for record in records:
                        record['_sdc_repository'] = repo_path
                        self.add_fields_at_1st_level(record = record, parent_record = None)

                        with singer.Transformer() as transformer:
                            if record.get(self.replication_keys):
                                if record[self.replication_keys] >= max_bookmark_value:
                                    # Update max_bookmark_value
                                    max_bookmark_value = record[self.replication_keys]

                                bookmark_dttm = record[self.replication_keys]

                                # Keep only records whose bookmark is after the last_datetime
                                if bookmark_dttm >= min_bookmark_value:
                                    if self.tap_stream_id in selected_stream_ids and bookmark_dttm >= parent_bookmark_value:
                                        rec = transformer.transform(record, stream_catalog['schema'], metadata=metadata.to_map(stream_catalog['metadata']))

                                        singer.write_record(self.tap_stream_id, rec, time_extracted=extraction_time)
                                        counter.increment()

                                    for child in self.children:
                                        if child in stream_to_sync:

                                            parent_id = tuple(record.get(key) for key in STREAMS[child]().id_keys)
                                            if STREAMS[child]().id_keys and not all(parent_id):
                                                pass
                                            else:
                                                # Sync child stream, if it is selected or its nested child is selected.
                                                self.get_child_records(client,
                                                                    catalog,
                                                                    child,
                                                                    parent_id,
                                                                    repo_path,
                                                                    state,
                                                                    start_date,
                                                                    record.get(self.replication_keys),
                                                                    stream_to_sync,
                                                                    selected_stream_ids,
                                                                    parent_record = record)
                                    # Write bookmark for incremental stream.
                                    self.write_bookmarks(self.tap_stream_id, selected_stream_ids, max_bookmark_value, repo_path, state)
                            else:
                                LOGGER.warning("Skipping this record for %s stream with %s = %s as it is missing replication key %s.",
                                            self.tap_stream_id, self.key_properties, record[self.key_properties], self.replication_keys)
                if max_bookmark_value < start_date: max_bookmark_value = start_date
                # Write bookmark for incremental stream.
                self.write_bookmarks(self.tap_stream_id, selected_stream_ids, max_bookmark_value, repo_path, state)
                singer.write_state(state) 

        return state

class IncrementalOrderedStream(Stream):

    def sync_endpoint(self,
                      client,
                      state,
                      catalog,
                      repo_path,
                      start_date,
                      selected_stream_ids,
                      stream_to_sync,
                      config,
                      ):
        """
        A sync function for streams that have records in the descending order of replication key value. For such streams,
        iterate only the latest records.
        """
        bookmark_value = get_bookmark(state, repo_path, self.tap_stream_id, "since", start_date)
        current_time = datetime.today().strftime(DATE_FORMAT)

        min_bookmark_value = self.get_min_bookmark(self.tap_stream_id, selected_stream_ids, current_time, repo_path, start_date, state)
        bookmark_time = singer.utils.strptime_to_utc(min_bookmark_value)

        # Build full url
        full_url = self.build_url(client.base_url, repo_path, bookmark_value)
        synced_all_records = False
        stream_catalog = get_schema(catalog, self.tap_stream_id)

        parent_bookmark_value = bookmark_value
        record_counter = 0
        with metrics.record_counter(self.tap_stream_id) as counter:
            for response in client.authed_get_all_pages(
                    self.tap_stream_id,
                    full_url,
                    stream = self.tap_stream_id
            ):
                records = response.json()
                if self.result_path: records = records.get(self.result_path,[])
                extraction_time = singer.utils.now()
                for record in records:
                    record['_sdc_repository'] = repo_path
                    self.add_fields_at_1st_level(record = record, parent_record = None)

                    updated_at = record.get(self.replication_keys)

                    if record_counter == 0 and updated_at > bookmark_value:
                        # Consider replication key value of 1st record as bookmark value.
                        # Because all records are in descending order of replication key value
                        bookmark_value = updated_at
                    record_counter = record_counter + 1

                    if updated_at:
                        if bookmark_time and singer.utils.strptime_to_utc(updated_at) < bookmark_time:
                            # Skip all records from now onwards because the bookmark value of the current record is less than
                            # last saved bookmark value and all records from now onwards will have bookmark value less than last
                            # saved bookmark value.
                            synced_all_records = True
                            break

                        if self.tap_stream_id in selected_stream_ids and updated_at >= parent_bookmark_value:

                            # Transform and write record
                            with singer.Transformer() as transformer:
                                rec = transformer.transform(record, stream_catalog['schema'], metadata=metadata.to_map(stream_catalog['metadata']))
                                singer.write_record(self.tap_stream_id, rec, time_extracted=extraction_time)
                                counter.increment()

                        for child in self.children:
                            if child in stream_to_sync:
                                parent_id = tuple(record.get(key) for key in STREAMS[child]().id_keys)
                                LOGGER.info(f"Syncing child {child}")

                                # Sync child stream, if it is selected or its nested child is selected.
                                self.get_child_records(client,
                                                    catalog,
                                                    child,
                                                    parent_id,
                                                    repo_path,
                                                    state,
                                                    start_date,
                                                    record.get(self.replication_keys),
                                                    stream_to_sync,
                                                    selected_stream_ids,
                                                    parent_record = record)
                        # Write bookmark for incremental stream.
                        self.write_bookmarks(self.tap_stream_id, selected_stream_ids, bookmark_value, repo_path, state)
                    else:
                        LOGGER.warning("Skipping this record for %s stream with %s = %s as it is missing replication key %s.",
                                    self.tap_stream_id, self.key_properties, record[self.key_properties], self.replication_keys)

                if synced_all_records:
                    break

            # Write bookmark for incremental stream.
            self.write_bookmarks(self.tap_stream_id, selected_stream_ids, bookmark_value, repo_path, state)

        return state

class Reviews(IncrementalStream):
    '''
    https://docs.github.com/en/rest/reference/pulls#list-reviews-for-a-pull-request
    '''
    tap_stream_id = "reviews"
    replication_method = "INCREMENTAL"
    replication_keys = "submitted_at"
    key_properties = ["id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "pulls/{}/reviews"
    use_repository = True
    id_keys = ['number']
    parent = 'pull_requests'

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        record['pr_id'] = parent_record['id']

class ReviewComments(IncrementalOrderedStream):
    '''
    https://docs.github.com/en/rest/pulls/comments#list-review-comments-on-a-pull-request
    '''
    tap_stream_id = "review_comments"
    replication_method = "INCREMENTAL"
    replication_keys = "updated_at"
    key_properties = ["id"]
    additional_filters = f"sort=updated_at&direction=asc&per_page{PER_PAGE_NUMBER}"
    path = "pulls/{}/comments"
    use_repository = True
    id_keys = ['number']
    parent = 'pull_requests'

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        record['pr_id'] = parent_record['id']

class PRCommits(IncrementalStream):
    '''
    https://docs.github.com/en/rest/reference/pulls#list-commits-on-a-pull-request
    '''
    tap_stream_id = "pr_commits"
    replication_method = "INCREMENTAL"
    replication_keys = "updated_at"
    key_properties = ["id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "pulls/{}/commits"
    use_repository = True
    id_keys = ['number']
    parent = 'pull_requests'

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        record['updated_at'] = record['commit']['committer']['date']

        record['pr_number'] = parent_record.get('number')
        record['pr_id'] = parent_record.get('id')
        record['id'] = '{}-{}'.format(parent_record.get('id'), record.get('sha'))

class PullRequests(IncrementalOrderedStream):
    '''
    https://developer.github.com/v3/pulls/#list-pull-requests
    '''
    tap_stream_id = "pull_requests"
    replication_method = "INCREMENTAL"
    replication_keys = "updated_at"
    key_properties = ["id"]
    additional_filters = f"state=all&sort=updated&direction=asc&per_page{PER_PAGE_NUMBER}"
    path = "pulls"
    children = ['reviews', 'review_comments', 'pr_commits']
    has_children = True
    pk_child_fields = ["number"]

class ProjectCards(IncrementalStream):
    '''
    https://docs.github.com/en/rest/reference/projects#list-project-cards
    '''
    tap_stream_id = "project_cards"
    replication_method = "INCREMENTAL"
    replication_keys = "updated_at"
    key_properties = ["id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "projects/columns/{}/cards"
    tap_stream_id = "project_cards"
    parent = 'project_columns'
    id_keys = ['id']

class ProjectColumns(IncrementalStream):
    '''
    https://docs.github.com/en/rest/reference/projects#list-project-columns
    '''
    tap_stream_id = "project_columns"
    replication_method = "INCREMENTAL"
    replication_keys = "updated_at"
    key_properties = ["id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "projects/{}/columns"
    children = ["project_cards"]
    parent = "projects"
    id_keys = ['id']
    has_children = True

class Projects(IncrementalStream):
    '''
    https://docs.github.com/en/rest/reference/projects#list-repository-projects
    '''
    tap_stream_id = "projects"
    replication_method = "INCREMENTAL"
    replication_keys = "updated_at"
    key_properties = ["id"]
    additional_filters = f"state=all&per_page{PER_PAGE_NUMBER}"
    path = "projects"
    tap_stream_id = "projects"
    children = ["project_columns"]
    has_children = True
    child_objects = [ProjectColumns()]

class TeamMemberships(FullTableStream):
    '''
    https://docs.github.com/en/rest/reference/teams#get-team-membership-for-a-user
    '''
    tap_stream_id = "team_memberships"
    replication_method = "FULL_TABLE"
    key_properties = ["url"]
    path = "orgs/{}/teams/{}/memberships/{}"
    use_organization = True
    parent = 'team_members'
    id_keys = ["login"]

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        record['login'] = parent_record['login']

class TeamMembers(FullTableStream):
    '''
    https://docs.github.com/en/rest/reference/teams#list-team-members
    '''
    tap_stream_id = "team_members"
    replication_method = "FULL_TABLE"
    key_properties = ["team_slug", "id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "orgs/{}/teams/{}/members"
    use_organization = True
    id_keys = ['slug']
    children= ["team_memberships"]
    has_children = True
    parent = 'teams'
    pk_child_fields = ['login']


    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        record['team_slug'] = parent_record['slug']

class Teams(FullTableStream):
    '''
    https://docs.github.com/en/rest/reference/teams#list-teams
    '''
    tap_stream_id = "teams"
    replication_method = "FULL_TABLE"
    key_properties = ["id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "orgs/{}/teams"
    use_organization = True
    children = ["team_members"]
    has_children = True
    pk_child_fields = ['slug']

class Commits(IncrementalDateStream):
    '''
    https://docs.github.com/en/rest/commits/commits#list-commits-on-a-repository
    '''
    tap_stream_id = "commits"
    replication_method = "INCREMENTAL"
    replication_keys = "updated_at"
    key_properties = ["sha"]
    path = "commits"
    children= ["commit_users_emails", "commit_files", "commit_parents", "commit_pull_request"]
    has_children = True
    since_filter_param_custom = "since={from}&until={until}&per_page=30"

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        if not record: return
        record['updated_at'] = self.get_field(record,['commit','committer','date'])
        record['message'] = self.get_field(record,['commit','message'])
        record['comit_name'] = self.get_field(record,['commit','committer','name'])
        record['author_email'] = self.get_field(record,['commit','author','email'])
        record['author_id'] = self.get_field(record,['author','id'])
        record['author_name'] = self.get_field(record,['commit','author','name'])
        record['author_login'] = self.get_field(record,['author','login'])
        record['committer_email'] = self.get_field(record,['commit','committer','email'])
        record['committer_name'] = self.get_field(record,['commit','committer','name'])

class CommitFiles(IncrementalStream):
    '''
    Child of "commits" - https://docs.github.com/en/rest/commits/commits#get-a-commit
    '''
    tap_stream_id = "commit_files"
    replication_method = "INCREMENTAL"
    key_properties = ["commit_sha", "filename"]
    id_keys = ["sha"]
    use_repository = True
    path = "commits/{}"
    inherit_parent_fields = [("commit_sha","sha"), ("_sdc_repository","_sdc_repository")]
    parent = 'commits'
    result_path = "files"

class CommitParents(FullTableStream):
    '''
    Child of "commits" - https://docs.github.com/en/rest/commits/commits#list-commits-on-a-repository
    '''
    tap_stream_id = "commit_parents"
    replication_method = "INCREMENTAL"
    key_properties = ["children_sha","sha"]
    no_path = True
    inherit_parent_fields = [("children_sha","sha"), ("_sdc_repository","_sdc_repository")]
    inherit_array_parent_fields = "parents"
    parent = 'commits'

class CommitPullRequest(IncrementalStream):
    '''
    https://docs.github.com/en/rest/commits/commits#list-pull-requests-associated-with-a-commit
    '''
    tap_stream_id = "commit_pull_request"
    replication_method = "INCREMENTAL"
    key_properties = ["commit_sha","pull_request_id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "commits/{}/pulls"
    use_repository = True
    id_keys = ["sha"]
    inherit_parent_fields = [("commit_sha","sha"), ("_sdc_repository","_sdc_repository")]
    parent = 'commits'

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        if not record: return
        record['pull_request_id'] = self.get_field(record,['id'])


class UserEmail(IncrementalStream):
    '''
    Created from fields of Commits table
    '''
    tap_stream_id = "commit_users_emails"
    replication_method = "INCREMENTAL"
    key_properties = ["email"]
    id_keys = ["author_email"]
    no_path = True
    inherit_parent_fields = [("email","author_email"),("id","author_id"),("name","author_name"),("username","author_login")]    
    parent = 'commits'

class Comments(IncrementalOrderedStream):
    '''
    https://docs.github.com/en/rest/issues/comments#list-comments-in-a-repository
    '''
    tap_stream_id = "comments"
    replication_method = "INCREMENTAL"
    replication_keys = "updated_at"
    key_properties = ["id"]
    since_filter_param = f"&sort=updated&direction=asc&per_page={PER_PAGE_NUMBER}"
    path = "issues/comments"

class Issues(IncrementalOrderedStream):
    '''
    https://docs.github.com/en/rest/issues/issues#list-repository-issues
    '''
    tap_stream_id = "issues"
    replication_method = "INCREMENTAL"
    replication_keys = "updated_at"
    key_properties = ["id"]
    since_filter_param = f"&state=all&sort=updated&direction=asc&per_page={PER_PAGE_NUMBER}"
    path = "issues"
    children = ["issue_assignees","issue_labels"]
    has_children = True

class IssueAssignees(IncrementalOrderedStream):
    '''
    Child of "issues" - https://docs.github.com/en/rest/issues/issues#list-repository-issues
    '''
    tap_stream_id = "issue_assignees"
    replication_method = "INCREMENTAL"
    key_properties = ["issue_id","id"]
    no_path = True
    inherit_parent_fields = [("issue_id","id"), ("_sdc_repository","_sdc_repository")]
    inherit_array_parent_fields = "assignees"
    parent = 'issues'

class IssueLabels(IncrementalOrderedStream):
    '''
    Child of "issues" - https://docs.github.com/en/rest/issues/issues#list-repository-issues
    '''
    tap_stream_id = "issue_labels"
    replication_method = "INCREMENTAL"
    key_properties = ["issue_id","id"]
    no_path = True
    inherit_parent_fields = [("issue_id","id"), ("_sdc_repository","_sdc_repository")]
    inherit_array_parent_fields = "labels"
    parent = 'issues'

class Assignees(FullTableStream):
    '''
    https://docs.github.com/en/rest/issues/assignees#list-assignees
    '''
    tap_stream_id = "assignees"
    replication_method = "FULL_TABLE"
    key_properties = ["id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "assignees"

class Releases(FullTableStream):
    '''
    https://docs.github.com/en/rest/releases/releases#list-releases
    '''
    tap_stream_id = "releases"
    replication_method = "FULL_TABLE"
    key_properties = ["id"]
    additional_filters = f"sort=created_at&direction=desc&per_page{PER_PAGE_NUMBER}"
    path = "releases"
    children = ["release_assets"]
    has_children = True

class ReleaseAssets(FullTableStream):
    '''
    Child of "releases" - https://docs.github.com/en/rest/releases/releases#list-releases
    '''
    tap_stream_id = "release_assets"
    replication_method = "FULL_TABLE"
    key_properties = ["id"]
    use_repository = True
    id_keys = ["id"]
    no_path = True
    inherit_parent_fields = [("release_id","id"), ("_sdc_repository","_sdc_repository")]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    inherit_array_parent_fields = "assets"
    parent = 'releases'

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        if not record: return
        record['uploader_id'] = self.get_field(record,['uploader','id'])

class Branches(FullTableStream):
    '''
    https://docs.github.com/en/rest/branches/branches#list-branches
    '''
    tap_stream_id = "branches"
    replication_method = "FULL_TABLE"
    key_properties = ["name"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "branches"

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        if not record: return
        record['commit_sha'] = self.get_field(record,['commit','sha'])
class Labels(FullTableStream):
    '''
    https://docs.github.com/en/rest/issues/labels#list-labels-for-a-repository
    '''
    tap_stream_id = "labels"
    replication_method = "FULL_TABLE"
    key_properties = ["id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "labels"

class IssueEvents(IncrementalOrderedStream):
    '''
    https://docs.github.com/en/rest/reference/issues#list-issue-events-for-a-repository
    '''
    tap_stream_id = "issue_events"
    replication_method = "INCREMENTAL"
    replication_keys = "created_at"
    key_properties = ["id"]
    additional_filters = f"sort=created_at&direction=desc&per_page{PER_PAGE_NUMBER}"
    path = "issues/events"

class Events(IncrementalStream):
    '''
    https://docs.github.com/en/rest/activity/events#list-repository-events
    '''
    tap_stream_id = "events"
    replication_method = "INCREMENTAL"
    replication_keys = "created_at"
    key_properties = ["id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "events"

class CommitComments(IncrementalStream):
    '''
    https://docs.github.com/en/rest/commits/comments#list-commit-comments-for-a-repository
    '''
    tap_stream_id = "commit_comments"
    replication_method = "INCREMENTAL"
    replication_keys = "updated_at"
    key_properties = ["id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "comments"

class IssueMilestones(IncrementalOrderedStream):
    '''
    https://docs.github.com/en/rest/issues/milestones#list-milestones
    '''
    tap_stream_id = "issue_milestones"
    replication_method = "INCREMENTAL"
    replication_keys = "updated_at"
    key_properties = ["id"]
    additional_filters = f"direction=desc&sort=updated_at&per_page{PER_PAGE_NUMBER}"
    path = "milestones"

class Collaborators(FullTableStream):
    '''
    https://docs.github.com/en/rest/collaborators/collaborators#list-repository-collaborators
    '''
    tap_stream_id = "collaborators"
    replication_method = "FULL_TABLE"
    key_properties = ["id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "collaborators"
    children = ["collaborator_details"]
    has_children = True

class CollaboratorDetails(FullTableStream):
    '''
    https://docs.github.com/en/rest/users/users#get-a-user
    '''
    tap_stream_id = "collaborator_details"
    replication_method = "FULL_TABLE"
    key_properties = ["id"]
    id_keys = ["login"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "users/{}"
    parent = 'collaborators'


class StarGazers(FullTableStream):
    '''
    https://docs.github.com/en/rest/activity/starring#list-stargazers
    '''
    tap_stream_id = "stargazers"
    replication_method = "FULL_TABLE"
    key_properties = ["user_id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "stargazers"
    headers = {'Accept': 'application/vnd.github.v3.star+json'}

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        record['user_id'] = record['user']['id']

class Repositories(FullTableStream):
    '''
    https://docs.github.com/en/rest/repos/repos#list-organization-repositories
    '''
    tap_stream_id = "repositories"
    replication_method = "FULL_TABLE"
    key_properties = ["id"]
    use_organization = True
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "orgs/{}/repos"
    children = ["repository_topics"]
    has_children = True

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        if not record: return
        record['owner_id'] = self.get_field(record,['owner','id'])

class RepositoryTeams(FullTableStream):
    '''
    https://docs.github.com/en/rest/repos/repos#list-repository-teams
    '''
    tap_stream_id = "repository_teams"
    replication_method = "FULL_TABLE"
    key_properties = ["_sdc_repository","id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "teams"

class RepositoryTopics(FullTableStream):
    '''
    Child of "repositories" - https://docs.github.com/en/rest/repos/repos#list-organization-repositories
    '''
    tap_stream_id = "repository_topics"
    replication_method = "FULL_TABLE"
    key_properties = ["repository","topic"]
    no_path = True
    id_keys = ["full_name"]
    inherit_parent_fields = [("repository","full_name")]
    inherit_array_parent_fields = "topics"
    custom_column_name = "topic"
    parent = 'repositories'

class Deployments(FullTableStream):
    '''
    https://docs.github.com/en/rest/deployments/deployments#list-deployments
    '''
    tap_stream_id = "deployments"
    replication_method = "FULL_TABLE"
    key_properties = ["id"]
    additional_filters = f"sort=created_at&direction=desc&per_page{PER_PAGE_NUMBER}"
    path = "deployments"
    children = ["deployment_statuses"]
    has_children = True

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        if not record: return
        record['creator_id'] = self.get_field(record,['creator','id'])

class DeploymentStatuses(FullTableStream):
    '''
    https://docs.github.com/en/rest/deployments/statuses#list-deployment-statuses
    '''
    tap_stream_id = "deployment_statuses"
    replication_method = "FULL_TABLE"
    use_repository = True
    key_properties = ["deployment_id","id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "deployments/{}/statuses"
    id_keys = ["id"]
    inherit_parent_fields = [("deployment_id","id"),("_sdc_repository","_sdc_repository")]
    parent = 'deployments'

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        if not record: return
        record['creator_id'] = self.get_field(record,['creator','id'])

class Workflows(FullTableStream):
    '''
    https://docs.github.com/en/rest/actions/workflows#list-repository-workflows
    '''
    tap_stream_id = "workflows"
    replication_method = "FULL_TABLE"
    key_properties = ["id"]
    additional_filters = f"per_page{PER_PAGE_NUMBER}"
    path = "actions/workflows"
    result_path = "workflows"

class WorkflowRuns(IncrementalDateStream):
    '''
    https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository
    '''
    tap_stream_id = "workflow_runs"
    replication_method = "INCREMENTAL"
    replication_keys = "created_at"
    use_repository = True
    key_properties = ["id"]
    path = "actions/runs"
    result_path = "workflow_runs"
    since_filter_param_custom = "per_page=100&created={from}..{until}"
    children = ["workflow_run_pull_requests"]
    has_children = True

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        if not record: return
        record['actor_id'] = self.get_field(record,['actor','id'])
        record['triggering_actor_id'] = self.get_field(record,['triggering_actor','id'])
        record['repository_id'] = self.get_field(record,['repository','id'])

class WorkflowPullRequests(IncrementalStream):
    '''
    Child of "workflow_runs" - https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository
    '''
    tap_stream_id = "workflow_run_pull_requests"
    replication_method = "INCREMENTAL"
    key_properties = ["workflow_run_id","id"]
    no_path = True
    inherit_parent_fields = [("workflow_run_id","id"), ("_sdc_repository","_sdc_repository")]
    inherit_array_parent_fields = "pull_requests"
    parent = 'workflow_runs'

    def add_fields_at_1st_level(self, record, parent_record = None):
        """
        Add fields in the record explicitly at the 1st level of JSON.
        """
        if not record: return
        record['head_sha'] = self.get_field(record,['head','sha'])
        record['base_sha'] = self.get_field(record,['base','sha'])

# Dictionary of the stream classes
STREAMS = {
    "repositories": Repositories,
    "repository_teams": RepositoryTeams,
    "repository_topics": RepositoryTopics,
    "commits": Commits,
    "commit_files": CommitFiles,
    "commit_parents": CommitParents,
    "commit_pull_request": CommitPullRequest,
    "comments": Comments,
    "issues": Issues,
    "issue_assignees": IssueAssignees,
    "issue_labels": IssueLabels,
    "assignees": Assignees,
    "releases": Releases,
    "release_assets": ReleaseAssets,
    "branches": Branches,
    "labels": Labels,
    "issue_events": IssueEvents,
    "events": Events,
    "commit_comments": CommitComments,
    "issue_milestones": IssueMilestones,
    "projects": Projects,
    "project_columns": ProjectColumns,
    "project_cards": ProjectCards,
    "pull_requests": PullRequests,
    "reviews": Reviews,
    "review_comments": ReviewComments,
    "pr_commits": PRCommits,
    "teams": Teams,
    "team_members": TeamMembers,
    "team_memberships": TeamMemberships,
    "collaborators": Collaborators,
    "collaborator_details": CollaboratorDetails,
    "stargazers": StarGazers,
    "commit_users_emails": UserEmail,
    "deployments": Deployments,
    "deployment_statuses": DeploymentStatuses,
    "workflows": Workflows,
    "workflow_runs": WorkflowRuns,
    "workflow_run_pull_requests": WorkflowPullRequests
}

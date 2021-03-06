"""Operators for handling database-related operations."""
import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Tuple, Union
from uuid import uuid4

from aiohttp import web
from dateutil.relativedelta import relativedelta
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCursor
from multidict import MultiDictProxy
from pymongo.errors import ConnectionFailure, OperationFailure

from ..conf.conf import query_map
from ..database.db_service import DBService, auto_reconnect
from ..helpers.logger import LOG
from ..helpers.parser import XMLToJSONParser
from ..helpers.validator import JSONValidator


class BaseOperator(ABC):
    """Base class for operators, implements shared functionality.

    :param ABC: The abstract base class
    """

    def __init__(self, db_name: str, content_type: str, db_client: AsyncIOMotorClient) -> None:
        """Init needed variables, must be given by subclass.

        :param db_name: Name for database to save objects to.
        :param content_type: Content type this operator handles (XML or JSON)
        :param db_client: Motor client used for database connections. Should be
        running on same loop with aiohttp, so needs to be passed from aiohttp
        Application.
        """
        self.db_service = DBService(db_name, db_client)
        self.content_type = content_type

    async def create_metadata_object(self, schema_type: str, data: Union[Dict, str]) -> str:
        """Create new metadata object to database.

        Data formatting and addition step for JSON or XML must be implemented
        by corresponding subclass.

        :param schema_type: Schema type of the object to create.
        :param data: Data to be saved to database.
        :returns: Accession id for the object inserted to database
        """
        accession_id = await self._format_data_to_create_and_add_to_db(schema_type, data)
        LOG.info(
            f"Inserting object with schema {schema_type} to database " f"succeeded with accession id: {accession_id}"
        )
        return accession_id

    async def replace_metadata_object(self, schema_type: str, accession_id: str, data: Union[Dict, str]) -> str:
        """Replace metadata object from database.

        Data formatting and addition step for JSON or XML must be implemented
        by corresponding subclass.

        :param schema_type: Schema type of the object to replace.
        :param accession_id: Identifier of object to replace.
        :param data: Data to be saved to database.
        :returns: Accession id for the object replaced to database
        """
        await self._format_data_to_replace_and_add_to_db(schema_type, accession_id, data)
        LOG.info(
            f"Replacing object with schema {schema_type} to database " f"succeeded with accession id: {accession_id}"
        )
        return accession_id

    async def update_metadata_object(self, schema_type: str, accession_id: str, data: Union[Dict, str]) -> str:
        """Update metadata object from database.

        Data formatting and addition step for JSON or XML must be implemented
        by corresponding subclass.

        :param schema_type: Schema type of the object to update.
        :param accession_id: Identifier of object to update.
        :param data: Data to be saved to database.
        :returns: Accession id for the object updated to database
        """
        await self._format_data_to_update_and_add_to_db(schema_type, accession_id, data)
        LOG.info(
            f"Updated object with schema {schema_type} to database " f"succeeded with accession id: {accession_id}"
        )
        return accession_id

    async def read_metadata_object(self, schema_type: str, accession_id: str) -> Tuple[Union[Dict, str], str]:
        """Read metadata object from database.

        Data formatting to JSON or XML must be implemented by corresponding
        subclass.

        :param schema_type: Schema type of the object to read.
        :param accession_id: Accession Id of the object to read.
        :raises: HTTPBadRequest if reading was not successful, HTTPNotFound if no data found
        :returns: Metadata object formatted to JSON or XML, content type
        """
        try:
            data_raw = await self.db_service.read(schema_type, accession_id)
            if not data_raw:
                LOG.error(f"Object with {accession_id} not found.")
                raise web.HTTPNotFound()
            data = await self._format_read_data(schema_type, data_raw)
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while getting object: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        return data, self.content_type

    async def delete_metadata_object(self, schema_type: str, accession_id: str) -> str:
        """Delete metadata object from database.

        Tries to remove both JSON and original XML from database, passes
        silently if objects don't exist in database.

        :param schema_type: Schema type of the object to delete.
        :param accession_id: Accession Id of the object to delete.
        :raises: HTTPBadRequest if deleting was not successful
        """
        db_client = self.db_service.db_client
        JSON_deletion_success = await self._remove_object_from_db(Operator(db_client), schema_type, accession_id)
        XML_deletion_success = await self._remove_object_from_db(XMLOperator(db_client), schema_type, accession_id)
        if JSON_deletion_success and XML_deletion_success:
            LOG.info(f"{accession_id} successfully deleted from collection")
            return accession_id
        else:
            reason = f"Deleting {accession_id} from database failed."
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

    async def _insert_formatted_object_to_db(self, schema_type: str, data: Dict) -> str:
        """Insert formatted metadata object to database.

        :param schema_type: Schema type of the object to insert.
        :param data: Single document formatted as JSON
        :returns: Accession Id for object inserted to database
        :raises: HTTPBadRequest if reading was not successful
        """
        try:
            insert_success = await self.db_service.create(schema_type, data)
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while getting object: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        if insert_success:
            return data["accessionId"]
        else:
            reason = "Inserting object to database failed for some reason."
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

    async def _replace_object_from_db(self, schema_type: str, accession_id: str, data: Dict) -> str:
        """Replace formatted metadata object in database.

        :param schema_type: Schema type of the object to replace.
        :param accession_id: Identifier of object to replace.
        :param data: Single document formatted as JSON
        :raises: HTTPBadRequest if reading was not successful, HTTPNotFound if no data found
        :returns: Accession Id for object inserted to database
        """
        try:
            check_exists = await self.db_service.exists(schema_type, accession_id)
            if not check_exists:
                reason = f"Object with accession id {accession_id} was not found."
                LOG.error(reason)
                raise web.HTTPNotFound(reason=reason)
            replace_success = await self.db_service.replace(schema_type, accession_id, data)
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while getting object: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        if replace_success:
            return accession_id
        else:
            reason = "Replacing object to database failed for some reason."
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

    async def _update_object_from_db(self, schema_type: str, accession_id: str, data: Dict) -> str:
        """Update formatted metadata object in database.

        After the data has been update we need to do a sanity check
        to see if the patched data still adheres to the corresponding
        JSON schema.

        :param schema_type: Schema type of the object to update.
        :param accession_id: Identifier of object to update.
        :param data: Single document formatted as JSON
        :raises: HTTPBadRequest if reading was not successful, HTTPNotFound if no data found
        :returns: Accession Id for object inserted to database
        """
        try:
            check_exists = await self.db_service.exists(schema_type, accession_id)
            if not check_exists:
                reason = f"Object with accession id {accession_id} was not found."
                LOG.error(reason)
                raise web.HTTPNotFound(reason=reason)
            update_success = await self.db_service.update(schema_type, accession_id, data)
            sanity_check = await self.db_service.read(schema_type, accession_id)
            # remove `draft-` from schema type
            if not schema_type.startswith("draft"):
                JSONValidator(sanity_check, schema_type).validate
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while getting object: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        if update_success:
            return accession_id
        else:
            reason = "Replacing object to database failed for some reason."
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

    async def _remove_object_from_db(self, operator: Any, schema_type: str, accession_id: str) -> bool:
        """Delete object from database.

        We can omit raising error for XMLOperator if id is not
        in backup collection.

        :param schema_type: Schema type of the object to delete.
        :param accession_id: Identifier of object to delete.
        :param data: Single document formatted as JSON
        :raises: HTTPBadRequest if reading was not successful, HTTPNotFound if no data found
        :returns: None
        """
        try:
            check_exists = await operator.db_service.exists(schema_type, accession_id)
            if not check_exists and not isinstance(operator, XMLOperator):
                reason = f"Object with accession id {accession_id} was not found."
                LOG.error(reason)
                raise web.HTTPNotFound(reason=reason)
            else:
                LOG.debug("XML is not in backup collection")
            delete_success = await operator.db_service.delete(schema_type, accession_id)
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while deleting object: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        return delete_success

    async def check_exists(self, schema_type: str, accession_id: str) -> None:
        """Check the existance of a object by its id in the database.

        :param schema_type: Schema type of the object to find.
        :param accession_id: Identifier of object to find.
        :raises: HTTPNotFound if object does not exist
        :returns: None
        """
        exists = await self.db_service.exists(schema_type, accession_id)
        LOG.info(f"check_exists: {exists}")
        if not exists:
            reason = f"Object with id {accession_id} from schema {schema_type} was not found."
            LOG.error(reason)
            raise web.HTTPNotFound(reason=reason)

    @abstractmethod
    async def _format_data_to_create_and_add_to_db(self, schema_type: str, data: Any) -> str:
        """Format and add data to database.

        Must be implemented by subclass.
        """

    @abstractmethod
    async def _format_data_to_replace_and_add_to_db(self, schema_type: str, accession_id: str, data: Any) -> str:
        """Format and replace data in database.

        Must be implemented by subclass.
        """

    @abstractmethod
    async def _format_data_to_update_and_add_to_db(self, schema_type: str, accession_id: str, data: Any) -> str:
        """Format and update data in database.

        Must be implemented by subclass.
        """

    @abstractmethod
    async def _format_read_data(self, schema_type: str, data_raw: Any) -> Any:
        """Format data for API response.

        Must be implemented by subclass.
        """


class Operator(BaseOperator):
    """Default operator class for handling database operations.

    Operations are implemented with JSON format.
    """

    def __init__(self, db_client: AsyncIOMotorClient) -> None:
        """Initialize database and content-type.

        :param db_client: Motor client used for database connections. Should be
        running on same loop with aiohttp, so needs to be passed from aiohttp
        Application.
        """
        super().__init__("objects", "application/json", db_client)

    async def query_metadata_database(
        self, schema_type: str, que: MultiDictProxy, page_num: int, page_size: int, filter_objects: List
    ) -> Tuple[List, int, int, int]:
        """Query database based on url query parameters.

        Url queries are mapped to mongodb queries based on query_map in
        apps config.

        :param schema_type: Schema type of the object to read.
        :param que: Dict containing query information
        :param page_size: Results per page
        :param page_num: Page number
        :param filter_objects: List of objects belonging to a user
        :raises: HTTPBadRequest if error happened when connection to database
        and HTTPNotFound error if object with given accession id is not found.
        :returns: Query result with pagination numbers
        """
        # Redact the query by checking the accessionId belongs to user
        redacted_content = {
            "$redact": {
                "$cond": {
                    "if": {"$in": ["$accessionId", filter_objects]} if len(filter_objects) > 1 else {},
                    "then": "$$DESCEND",
                    "else": "$$PRUNE",
                }
            }
        }
        # Generate mongodb query from query parameters
        mongo_query: Dict[Any, Any] = {}
        for query, value in que.items():
            if query in query_map:
                regx = re.compile(f".*{value}.*", re.IGNORECASE)
                if isinstance(query_map[query], dict):
                    # Make or-query for keys in dictionary
                    base = query_map[query]["base"]  # type: ignore
                    if "$or" not in mongo_query:
                        mongo_query["$or"] = []
                    for key in query_map[query]["keys"]:  # type: ignore
                        if value.isdigit():
                            regi = {
                                "$expr": {
                                    "$regexMatch": {
                                        "input": {"$toString": f"${base}.{key}"},
                                        "regex": f".*{int(value)}.*",
                                    }
                                }
                            }
                            mongo_query["$or"].append(regi)
                        else:
                            mongo_query["$or"].append({f"{base}.{key}": regx})
                else:
                    # Query with regex from just one field
                    mongo_query = {query_map[query]: regx}
        LOG.debug(f"Query construct: {mongo_query}")
        LOG.debug(f"redacted filter: {redacted_content}")
        skips = page_size * (page_num - 1)
        aggregate_query = [
            {"$match": mongo_query},
            redacted_content,
            {"$skip": skips},
            {"$limit": page_size},
            {"$project": {"_id": 0}},
        ]
        try:
            result_aggregate = await self.db_service.aggregate(schema_type, aggregate_query)
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while getting object: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        data = await self._format_read_data(schema_type, result_aggregate)

        if not data:
            reason = f"could not find any data in {schema_type}."
            LOG.error(reason)
            raise web.HTTPNotFound(reason=reason)

        page_size = len(data) if len(data) != page_size else page_size
        count_query = [{"$match": mongo_query}, redacted_content, {"$count": "total"}]
        total_objects = await self.db_service.aggregate(schema_type, count_query)

        LOG.debug(f"DB query: {que}")
        LOG.info(
            f"DB query successful for query on {schema_type} "
            f"resulted in {total_objects[0]['total']}. "
            f"Requested was page {page_num} and page size {page_size}."
        )
        return data, page_num, page_size, total_objects[0]["total"]

    async def _format_data_to_create_and_add_to_db(self, schema_type: str, data: Dict) -> str:
        """Format JSON metadata object and add it to db.

        Adds necessary additional information to object before adding to db.

        If schema type is study, publishDate and status is added.
        By default date is two months from submission date (based on ENA
        submission model).

        :param schema_type: Schema type of the object to create.
        :param data: Metadata object
        :returns: Accession Id for object inserted to database
        """
        accession_id = self._generate_accession_id()
        data["accessionId"] = accession_id
        data["dateCreated"] = datetime.utcnow()
        data["dateModified"] = datetime.utcnow()
        if schema_type == "study":
            data["publishDate"] = datetime.utcnow() + relativedelta(months=2)
        LOG.debug(f"Operator formatted data for {schema_type} to add to DB.")
        return await self._insert_formatted_object_to_db(schema_type, data)

    async def _format_data_to_replace_and_add_to_db(self, schema_type: str, accession_id: str, data: Dict) -> str:
        """Format JSON metadata object and replace it in db.

        Replace information to object before adding to db.

        We will not replace accessionId, publishDate or dateCreated,
        as these should are generated when created.

        We will keep also publisDate and dateCreated from old object.

        :param schema_type: Schema type of the object to replace.
        :param accession_id: Identifier of object to replace.
        :param data: Metadata object
        :returns: Accession Id for object inserted to database
        """
        forbidden_keys = ["accessionId", "publishDate", "dateCreated"]
        if any(i in data for i in forbidden_keys):
            reason = f"Some items (e.g: {', '.join(forbidden_keys)}) cannot be changed."
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        data["accessionId"] = accession_id
        data["dateModified"] = datetime.utcnow()
        LOG.debug(f"Operator formatted data for {schema_type} to add to DB")
        return await self._replace_object_from_db(schema_type, accession_id, data)

    async def _format_data_to_update_and_add_to_db(self, schema_type: str, accession_id: str, data: Any) -> str:
        """Format and update data in database.

        :param schema_type: Schema type of the object to replace.
        :param accession_id: Identifier of object to replace.
        :param data: Metadata object
        :returns: Accession Id for object inserted to database
        """
        forbidden_keys = ["accessionId", "publishDate", "dateCreated"]
        if any(i in data for i in forbidden_keys):
            reason = f"Some items (e.g: {', '.join(forbidden_keys)}) cannot be changed."
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        data["accessionId"] = accession_id
        data["dateModified"] = datetime.utcnow()
        LOG.debug(f"Operator formatted data for {schema_type} to add to DB")
        return await self._update_object_from_db(schema_type, accession_id, data)

    def _generate_accession_id(self) -> str:
        """Generate random accession id.

        Will be replaced later with external id generator.
        """
        sequence = uuid4().hex
        LOG.debug("Generated accession ID.")
        return sequence

    @auto_reconnect
    async def _format_read_data(
        self, schema_type: str, data_raw: Union[Dict, AsyncIOMotorCursor]
    ) -> Union[Dict, List[Dict]]:
        """Get JSON content from given mongodb data.

        Data can be either one result or cursor containing multiple
        results.

        If data is cursor, the query it contains is executed here and possible
        database connection failures are try-catched with reconnect decorator.

        :param schema_type: Schema type of the object to read.
        :param data_raw: Data from mongodb query, can contain multiple results
        :returns: Mongodb query result, formatted to readable dicts
        """
        if isinstance(data_raw, dict):
            return self._format_single_dict(schema_type, data_raw)
        else:
            return [self._format_single_dict(schema_type, doc) for doc in data_raw]

    def _format_single_dict(self, schema_type: str, doc: Dict) -> Dict:
        """Format single result dictionary.

        Delete mongodb internal id from returned result.
        For studies, publish date is formatted to ISO 8601.

        :param schema_type: Schema type of the object to read.
        :param doc: single document from mongodb
        :returns: formatted version of document
        """

        def format_date(key: str, doc: Dict) -> Dict:
            doc[key] = doc[key].isoformat()
            return doc

        doc = format_date("dateCreated", doc)
        doc = format_date("dateModified", doc)
        if schema_type == "study":
            doc = format_date("publishDate", doc)
        return doc


class XMLOperator(BaseOperator):
    """Alternative operator class for handling database operations.

    Operations are implemented with XML format.
    """

    def __init__(self, db_client: AsyncIOMotorClient) -> None:
        """Initialize database and content-type.

        :param db_client: Motor client used for database connections. Should be
        running on same loop with aiohttp, so needs to be passed from aiohttp
        Application.
        """
        super().__init__("backups", "text/xml", db_client)

    async def _format_data_to_create_and_add_to_db(self, schema_type: str, data: str) -> str:
        """Format XML metadata object and add it to db.

        XML is validated, then parsed to json and json is added to database.
        After successful json insertion, xml itself is backed up to database.

        :param schema_type: Schema type of the object to read.
        :param data: Original xml content
        :returns: Accession Id for object inserted to database
        """
        db_client = self.db_service.db_client
        # remove `drafs-` from schema type
        schema = schema_type[6:] if schema_type.startswith("draft") else schema_type
        data_as_json = XMLToJSONParser().parse(schema, data)
        accession_id = await Operator(db_client)._format_data_to_create_and_add_to_db(schema_type, data_as_json)
        LOG.debug(f"XMLOperator formatted data for {schema_type} to add to DB")
        return await self._insert_formatted_object_to_db(schema_type, {"accessionId": accession_id, "content": data})

    async def _format_data_to_replace_and_add_to_db(self, schema_type: str, accession_id: str, data: str) -> str:
        """Format XML metadata object and add it to db.

        XML is validated, then parsed to json and json is added to database.
        After successful json insertion, xml itself is backed up to database.

        :param schema_type: Schema type of the object to replace.
        :param accession_id: Identifier of object to replace.
        :param data: Original xml content
        :returns: Accession Id for object inserted to database
        """
        db_client = self.db_service.db_client
        # remove `draft-` from schema type
        schema = schema_type[6:] if schema_type.startswith("draft") else schema_type
        data_as_json = XMLToJSONParser().parse(schema, data)
        accession_id = await Operator(db_client)._format_data_to_replace_and_add_to_db(
            schema_type, accession_id, data_as_json
        )
        LOG.debug(f"XMLOperator formatted data for {schema_type} to add to DB")
        return await self._replace_object_from_db(
            schema_type, accession_id, {"accessionId": accession_id, "content": data}
        )

    async def _format_data_to_update_and_add_to_db(self, schema_type: str, accession_id: str, data: str) -> str:
        """Raise not implemented.

        Patch update for XML not supported

        :param schema_type: Schema type of the object to replace.
        :param accession_id: Identifier of object to replace.
        :param data: Original xml content
        :raises: HTTPUnsupportedMediaType
        """
        reason = "XML patching is not possible."
        raise web.HTTPUnsupportedMediaType(reason=reason)

    async def _format_read_data(self, schema_type: str, data_raw: Dict) -> str:
        """Get XML content from given mongodb data.

        :param schema_type: Schema type of the object to read.
        :param data_raw: Data from mongodb query with single result.
        :returns: XML content
        """
        return data_raw["content"]


class FolderOperator:
    """Operator class for handling database operations of folders.

    Operations are implemented with JSON format.
    """

    def __init__(self, db_client: AsyncIOMotorClient) -> None:
        """Init db_service.

        :param db_client: Motor client used for database connections. Should be
        running on same loop with aiohttp, so needs to be passed from aiohttp
        Application.
        """
        self.db_service = DBService("folders", db_client)

    async def check_object_in_folder(self, collection: str, accession_id: str) -> Tuple[bool, str, bool]:
        """Check a object/draft is in a folder.

        :param collection: collection it belongs to, it would be used as path
        :param accession_id: document by accession_id
        :raises: HTTPUnprocessableEntity if error occurs during the process and object in more than 1 folder
        :returns: True for the check, folder id and if published or not
        """
        try:
            folder_path = "drafts" if collection.startswith("draft") else "metadataObjects"
            folder_query = {folder_path: {"$elemMatch": {"accessionId": accession_id, "schema": collection}}}

            folder_cursor = self.db_service.query("folder", folder_query)
            folder_check = [folder async for folder in folder_cursor]
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while inserting user: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

        if len(folder_check) == 0:
            LOG.info(f"doc {accession_id} belongs to no folder something is off")
            return False, "", False
        elif len(folder_check) > 1:
            reason = f"The {accession_id} is in more than 1 folder."
            LOG.error(reason)
            raise web.HTTPUnprocessableEntity(reason=reason)
        else:
            folder_id = folder_check[0]["folderId"]
            LOG.info(f"found doc {accession_id} in {folder_id}")
            return True, folder_id, folder_check[0]["published"]

    async def get_collection_objects(self, folder_id: str, collection: str) -> List:
        """List objects ids per collection.

        :param collection: collection it belongs to, it would be used as path
        :returns: count of objects
        """
        try:
            folder_path = "drafts" if collection.startswith("draft") else "metadataObjects"
            folder_query = {"$and": [{folder_path: {"$elemMatch": {"schema": collection}}}, {"folderId": folder_id}]}

            folder_cursor = self.db_service.query("folder", folder_query)
            folders = [folder async for folder in folder_cursor]
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while inserting user: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

        if len(folders) >= 1:
            return [i["accessionId"] for i in folders[0][folder_path]]
        else:
            return []

    async def create_folder(self, data: Dict) -> str:
        """Create new object folder to database.

        :param data: Data to be saved to database
        :raises: HTTPBadRequest if error occurs during the process of insert
        :returns: Folder id for the folder inserted to database
        """
        folder_id = self._generate_folder_id()
        data["folderId"] = folder_id
        data["published"] = False
        data["metadataObjects"] = data["metadataObjects"] if "metadataObjects" in data else []
        data["drafts"] = data["drafts"] if "drafts" in data else []
        try:
            insert_success = await self.db_service.create("folder", data)
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while inserting folder: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

        if not insert_success:
            reason = "Inserting folder to database failed for some reason."
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        else:
            LOG.info(f"Inserting folder with id {folder_id} to database succeeded.")
            return folder_id

    @auto_reconnect
    async def query_folders(self, que: Dict) -> List:
        """Query database based on url query parameters.

        :param que: Dict containing query information
        :returns: Query result as list
        """
        folders = await self.db_service.aggregate("folder", que)
        return folders

    async def read_folder(self, folder_id: str) -> Dict:
        """Read object folder from database.

        :param folder_id: Folder ID of the object to read
        :raises: HTTPBadRequest if reading was not successful
        :returns: Object folder formatted to JSON
        """
        try:
            await self.check_folder_exists(folder_id)
            folder = await self.db_service.read("folder", folder_id)
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while getting folder: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        return folder

    async def update_folder(self, folder_id: str, patch: List) -> str:
        """Update object folder from database.

        Utilizes JSON Patch operations specified at: http://jsonpatch.com/

        :param folder_id: ID of folder to update
        :param patch: JSON Patch operations determined in the request
        :raises: HTTPBadRequest if updating was not successful
        :returns: ID of the folder updated to database
        """
        try:
            await self.check_folder_exists(folder_id)
            update_success = await self.db_service.patch("folder", folder_id, patch)
            sanity_check = await self.db_service.read("folder", folder_id)
            JSONValidator(sanity_check, "folders").validate
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while getting folder: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

        if not update_success:
            reason = "Updating folder to database failed for some reason."
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        else:
            LOG.info(f"Updating folder with id {folder_id} to database succeeded.")
            return folder_id

    async def remove_object(self, folder_id: str, collection: str, accession_id: str) -> None:
        """Remove object from folders in the database.

        :param folder_id: ID of folder to update
        :param accession_id: ID of object to remove
        :param collection: collection where to remove the id from
        :raises: HTTPBadRequest if db connection fails
        :returns: None
        """
        try:
            await self.check_folder_exists(folder_id)
            folder_path = "drafts" if collection.startswith("draft") else "metadataObjects"
            upd_content = {folder_path: {"accessionId": accession_id}}
            result = await self.db_service.remove("folder", folder_id, upd_content)
            JSONValidator(result, "folders").validate
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while getting user: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

        LOG.info(f"Removing object {accession_id} from {folder_id} succeeded.")

    async def delete_folder(self, folder_id: str) -> str:
        """Delete object folder from database.

        :param folder_id: ID of the folder to delete.
        :raises: HTTPBadRequest if deleting was not successful
        :returns: ID of the folder deleted from database
        """
        try:
            await self.check_folder_exists(folder_id)
            delete_success = await self.db_service.delete("folder", folder_id)
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while deleting folder: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        if not delete_success:
            reason = f"Deleting for {folder_id} from database failed."
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        else:
            LOG.info(f"Deleting folder with id {folder_id} to database succeeded.")
            return folder_id

    async def check_folder_exists(self, folder_id: str) -> None:
        """Check the existance of a folder by its id in the database.

        :raises: HTTPNotFound if folder does not exist
        :returns: None
        """
        exists = await self.db_service.exists("folder", folder_id)
        if not exists:
            reason = f"Folder with id {folder_id} was not found."
            LOG.error(reason)
            raise web.HTTPNotFound(reason=reason)

    def _generate_folder_id(self) -> str:
        """Generate random folder id.

        :returns: str with folder id
        """
        sequence = uuid4().hex
        LOG.debug("Generated folder ID.")
        return sequence


class UserOperator:
    """Operator class for handling database operations of users.

    Operations are implemented with JSON format.
    """

    def __init__(self, db_client: AsyncIOMotorClient) -> None:
        """Init db_service.

        :param db_client: Motor client used for database connections. Should be
        running on same loop with aiohttp, so needs to be passed from aiohttp
        Application.
        """
        self.db_service = DBService("users", db_client)

    async def check_user_has_doc(self, collection: str, user_id: str, accession_id: str) -> bool:
        """Check a folder/draft belongs to user.

        :param collection: collection it belongs to, it would be used as path
        :param user_id: user_id from session
        :param accession_id: document by accession_id
        :raises: HTTPUnprocessableEntity if more users seem to have same folder
        :returns: True if accession_id belongs to user
        """
        try:
            doc_path = "drafts" if collection.startswith("draft") else "folders"
            user_query = {doc_path: {"$elemMatch": {"$eq": accession_id}}, "userId": user_id}
            user_cursor = self.db_service.query("user", user_query)
            user_check = [user async for user in user_cursor]
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while inserting user: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

        if len(user_check) == 0:
            LOG.info(f"doc {accession_id} belongs to no user something is off")
            return False
        elif len(user_check) > 1:
            reason = "There seem to be more users with same ID and/or same folders."
            LOG.error(reason)
            raise web.HTTPUnprocessableEntity(reason=reason)
        else:
            LOG.info(f"found doc {accession_id} at current user")
            return True

    async def create_user(self, data: Tuple) -> str:
        """Create new user object to database.

        :param data: User Data to identify user
        :raises: HTTPBadRequest if error occurs during the process of creating user
        :returns: User id for the user object inserted to database
        """
        user_data: Dict[str, Union[list, str]] = dict()

        eppn = data[0]
        name = data[1]
        try:
            existing_user_id = await self.db_service.exists_eppn_user(eppn, name)
            if existing_user_id:
                LOG.info(f"User with eppn: {eppn} exists, no need to create.")
                return existing_user_id
            else:
                user_data["drafts"] = []
                user_data["folders"] = []
                user_data["userId"] = user_id = self._generate_user_id()
                user_data["name"] = name
                user_data["eppn"] = eppn
                insert_success = await self.db_service.create("user", user_data)
                if not insert_success:
                    reason = "Inserting user to database failed for some reason."
                    LOG.error(reason)
                    raise web.HTTPBadRequest(reason=reason)
                else:
                    LOG.info(f"Inserting user with id {user_id} to database succeeded.")
                    return user_id
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while inserting user: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

    async def read_user(self, user_id: str) -> Dict:
        """Read user object from database.

        :param user_id: User ID of the object to read
        :raises: HTTPBadRequest if reading user was not successful
        :returns: User object formatted to JSON
        """
        try:
            await self._check_user_exists(user_id)
            user = await self.db_service.read("user", user_id)
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while getting user: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        return user

    async def update_user(self, user_id: str, patch: List) -> str:
        """Update user object from database.

        :param user_id: ID of user to update
        :param patch: Patch operations determined in the request
        :returns: ID of the user updated to database
        """
        try:
            await self._check_user_exists(user_id)
            update_success = await self.db_service.patch("user", user_id, patch)
            sanity_check = await self.db_service.read("user", user_id)
            JSONValidator(sanity_check, "users").validate
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while getting user: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

        if not update_success:
            reason = "Updating user to database failed for some reason."
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        else:
            LOG.info(f"Updating user with id {user_id} to database succeeded.")
            return user_id

    async def assign_objects(self, user_id: str, collection: str, object_ids: List) -> None:
        """Assing object to user.

        An object can be folder(s) or draft(s).

        :param user_id: ID of user to update
        :param collection: collection where to remove the id from
        :param object_ids: ID or list of IDs of folder(s) to assign
        :raises: HTTPBadRequest if assigning drafts/folders to user was not successful
        returns: None
        """
        try:
            await self._check_user_exists(user_id)
            upd_content = {collection: {"$each": object_ids}}
            result = await self.db_service.append("user", user_id, upd_content)
            JSONValidator(result, "users").validate
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while getting user: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        except Exception as e:
            reason = f"Updating user to database failed beacause of: {e}."
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

        LOG.info(f"Assigning {object_ids} from {user_id} succeeded.")

    async def remove_objects(self, user_id: str, collection: str, object_ids: List) -> None:
        """Remove object from user.

        An object can be folder(s) or draft(s).

        :param user_id: ID of user to update
        :param collection: collection where to remove the id from
        :param object_ids: ID or list of IDs of folder(s) to remove
        :raises: HTTPBadRequest if db connection fails
        returns: None
        """
        try:
            await self._check_user_exists(user_id)
            upd_content = {collection: {"$in": object_ids}}
            result = await self.db_service.remove("user", user_id, upd_content)
            JSONValidator(result, "users").validate
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while getting user: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)

        LOG.info(f"Removing {object_ids} from {user_id} succeeded.")

    async def delete_user(self, user_id: str) -> str:
        """Delete user object from database.

        :param user_id: ID of the user to delete.
        :raises: HTTPBadRequest if deleting user was not successful
        :returns: ID of the user deleted from database
        """
        try:
            await self._check_user_exists(user_id)
            delete_success = await self.db_service.delete("user", user_id)
        except (ConnectionFailure, OperationFailure) as error:
            reason = f"Error happened while deleting user: {error}"
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        if not delete_success:
            reason = "Deleting for {user_id} from database failed."
            LOG.error(reason)
            raise web.HTTPBadRequest(reason=reason)
        else:
            LOG.info(f"{user_id} successfully deleted from collection.")
            return user_id

    async def _check_user_exists(self, user_id: str) -> None:
        """Check the existance of a user by its id in the database.

        :param user_id: Identifier of user to find.
        :raises: HTTPNotFound if user does not exist
        :returns: None
        """
        exists = await self.db_service.exists("user", user_id)
        if not exists:
            reason = f"User with id {user_id} was not found."
            LOG.error(reason)
            raise web.HTTPNotFound(reason=reason)

    def _generate_user_id(self) -> str:
        """Generate random user id.

        :returns: str with user id
        """
        sequence = uuid4().hex
        LOG.debug("Generated user ID.")
        return sequence

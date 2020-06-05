"""Test api endpoints from views module."""
from pathlib import Path

from metadata_backend.helpers.parser import XMLToJSONParser
from aiohttp import web
from metadata_backend.helpers.schema_loader import SchemaLoader
import unittest
import datetime

from unittest.mock import patch


class ParserTestCase(unittest.TestCase):
    """Api endpoint class test cases."""

    TESTFILES_ROOT = Path(__file__).parent / 'test_files'

    def setUp(self):
        """Configure variables for tests."""
        self.parser = XMLToJSONParser()

    def load_xml_from_file(self, submission, filename):
        """Load xml as string from given file."""
        path_to_xml_file = self.TESTFILES_ROOT / submission / filename
        return path_to_xml_file.read_text()

    @patch('metadata_backend.helpers.parser.datetime')
    def test_study_is_parsed(self, mocked_datetime):
        """Test that study is parsed correctly and accessionId is set.

        Tests for some values that converted json should have.
        """
        mocked_datetime.utcnow.return_value = datetime.datetime(2020, 4, 14)
        study_xml = self.load_xml_from_file("study", "SRP000539.xml")
        study_json = self.parser.parse("study", study_xml)
        self.assertEqual(datetime.datetime(2020, 6, 14, 0, 0),
                         study_json['publishDate'])
        self.assertIn("Highly integrated epigenome maps in Arabidopsis",
                      study_json['descriptor']['studyTitle'])
        self.assertIn("18423832", study_json['studyLinks']['studyLink'][
            'xrefLink']['id'])

    def test_sample_is_parsed(self):
        """Test that sample is parsed correctly and accessionId is set.

        Tests for some values that converted json should have.
        """
        sample_xml = self.load_xml_from_file("sample", "SRS001433.xml")
        sample_json = self.parser.parse("sample", sample_xml)
        self.assertIn("Human HapMap individual NA18758",
                      sample_json['description'])
        self.assertIn("Homo sapiens",
                      sample_json['sampleName']['scientificName'])

    def test_experiment_is_parsed(self):
        """Test that experiment is parsed correctly and accessionId is set.

        Tests for some values that convert json should have.
        """
        experiment_xml = self.load_xml_from_file("experiment", "ERX000119.xml")
        experiment_json = self.parser.parse("experiment", experiment_xml)
        self.assertIn("SOLiD sequencing of Human HapMap individual NA18504",
                      experiment_json['design']['designDescription'])

    def test_run_is_parsed(self):
        """Test that run is parsed correctly and accessionId is set.

        Tests for some values that convert json should have.
        """
        run_xml = self.load_xml_from_file("run", "ERR000076.xml")
        run_json = self.parser.parse("run", run_xml)
        self.assertIn("ERA000/ERA000014/srf/BGI-FC304RWAAXX_5.srf",
                      run_json['dataBlock']['files']['file']['attributes'][
                          'filename'])
        self.assertIn("ERX000037", run_json['experimentRef']['attributes'][
            'accession'])

    def test_analysis_is_parsed(self):
        """Test that run is parsed correctly and accessionId is set.

        Tests for some values that convert json should have.
        """
        analysis_xml = self.load_xml_from_file("analysis", "ERZ266973.xml")
        analysis_json = self.parser.parse("analysis", analysis_xml)
        self.assertIn("GCA_000001405.1", analysis_json['analysisType'][
            'processedReads']['assembly']['standard']['attributes'][
            'accession'])

    def test_error_raised_when_schema_not_found(self):
        """Test 400 is returned when schema."""
        with self.assertRaises(web.HTTPBadRequest):
            self.parser._load_schema("None")

    def test_error_raised_when_validate_fails_against_schema(self):
        """Create sample validator, which should fail to validate study."""
        loader = SchemaLoader()
        schema = loader.get_schema("sample")
        with self.assertRaises(web.HTTPBadRequest):
            self.parser._validate("<STUDY_SET></STUDY_SET>", schema)

    def test_error_raised_when_input_xml_not_valid_xml(self):
        """Give validator xml with broken syntax, should fail."""
        loader = SchemaLoader()
        schema = loader.get_schema("study")
        study_xml = self.load_xml_from_file("study", "SRP000539_invalid.xml")
        with self.assertRaises(web.HTTPBadRequest):
            self.parser._validate(study_xml, schema)

    def test_empty_lists_are_removed_from_json(self):
        """Check empty lists are removed and non-empty are retained."""
        data = {'file': {'attributes': {
            'checksum': '3dfebb4b30211523853805439fbd7cec',
            'checksumMethod': 'MD5',
            'filetype': 'srf'},
            'children': []},
            'identifiers': {'primaryId': 'ERR000076',
                            'submitterId': {
                                'attributes': {'namespace': 'BGI'},
                                'children': ['BGI-FC304RWAAXX']}}}
        cleaned = self.parser._to_lowercase(data)
        self.assertTrue("children" not in cleaned['file']['attributes'])
        self.assertTrue("children" in cleaned['identifiers']['submitterid'])

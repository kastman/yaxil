import io
import os
import csv
import sys
import gzip
import json
import time
import random
import sqlite3
import zipfile
import logging
import requests
import itertools
import tempfile as tf
import collections as col
import xml.etree.ElementTree as etree
from argparse import Namespace
import yaxil.commons as commons
import yaxil.functools as functools
from .exceptions import (AuthError, MultipleAccessionError,  NullAccessionError,
                         AccessionError, DownloadError, ResultSetError,
                         ScanSearchError, EQCNotFoundError, RestApiError,
                         AutoboxError)

# Whether to verify SSL certificates. Primarily of use during testing.
CHECK_CERTIFICATE = True

logger = logging.getLogger(__name__)
logging.getLogger("requests").setLevel(logging.WARNING)

class Format(object):
    '''
    A container to hold possible API response formats Format.JSON, 
    Format.XML, and Format.CSV.
    '''
    JSON  = "json"
    XML   = "xml"
    CSV   = "csv"

XnatAuth = col.namedtuple("XnatAuth", [
    "url",
    "username",
    "password"
])
'''
Container to hold XNAT authentication information. Fields include the url, 
username, and password.
'''

@functools.lru_cache
def auth(alias, cfg="~/.xnat_auth"):
    '''
    Read connection details from properly formatted xnat_auth XML file.

    Example:
        >>> import yaxil
        >>> auth = yaxil.auth('xnatastic')
        >>> auth.url, auth.username, auth.password
        ('https://www.xnatastic.org/', 'username', '********')

    :param alias: XNAT alias
    :type alias: str
    :param cfg: Configuration file
    :type cfg: str
    :returns: Named tuple of (url, username, password)
    :rtype: :mod:`yaxil.XnatAuth`
    '''
    # check and parse config file
    cfg = os.path.expanduser(cfg)
    if not os.path.exists(cfg):
        raise AuthError("could not locate auth file %s" % cfg)
    tree = etree.parse(os.path.expanduser(cfg))
    # get alias
    if not alias:
        raise AuthError("alias cannot be empty")
    res = tree.findall("./%s" % alias.lower())
    if not res:
        raise AuthError("could not find alias %s in %s" % (alias, cfg))
    elif len(res) > 1:
        raise AuthError("too many %s's found in %s" % (alias, cfg))
    res = res.pop()
    # get url
    url = res.findall("url")
    if not url:
        raise AuthError("no url for %s in %s" % (alias, cfg))
    elif len(url) > 1:
        raise AuthError("too many urls for %s in %s" % (alias, cfg))
    # get username
    username = res.findall("username")
    if not username:
        raise AuthError("no username for %s in %s" % (alias, cfg))
    elif len(username) > 1:
        raise AuthError("too many usernames for %s in %s" % (alias, cfg))
    # get password
    password = res.findall("password")
    if not password:
        raise AuthError("no password for %s in %s" % (alias, cfg))
    elif len(password) > 1:
        raise AuthError("too many passwords for %s in %s" % (alias, cfg))
    return XnatAuth(url=url.pop().text, username=username.pop().text, 
                        password=password.pop().text)

Subject = col.namedtuple("Subject", [
    "uri",
    "label",
    "id",
    "project",
    "experiments"
])
'''
Container to hold XNAT Subject information. Fields include the URI (uri), 
Accession ID (id), Project (project), Label (label), and Experiments 
(experiments).
'''


def subject(auth, label, project=None):
    '''
    Get the URI, Accession ID, Label, Project, and all Experiment IDs for any 
    Subject label. If the Subject label is tied to mulitple Accession IDs, a 
    Project argument must be specified.
    
    Example:
        >>> import yaxil
        >>> auth = yaxil.XnatAuth(url='...', username='...', password='...')
        >>> yaxil.subject(auth, 'AB1234C')
        Subject(uri=u'/data/experiments/XNAT_S0001', label=u'AB1234C', id=u'XNAT_S0001', 
            project=u'MyProject', experiments=[u'XNAT_E0001', u'XNAT_E0002'])

    :param auth: XNAT authentication
    :type auth: :mod:`yaxil.XnatAuth`
    :param label: XNAT Subject label
    :type label: str
    :param project: XNAT Subject Project
    :type project: str
    :returns: Accession ID
    :rtype: str
    '''
    if not label:
        raise AccessionError("label cannot be empty")
    url = "%s/data/subjects" % auth.url.rstrip('/')
    logger.debug("issuing http request %s", url)
    payload = {"label": label, 
               "columns": "ID,label,project,xnat:mrsessiondata/id,xnat:mrsessiondata/label"}
    if project:
        payload["project"] = project
    r = requests.get(url, params=payload, auth=(auth.username, auth.password), 
                     verify=CHECK_CERTIFICATE)
    if r.status_code != requests.codes.ok:
        raise AccessionError("response not ok (%s) from %s" % (r.status_code, r.url))
    try:
        results = r.json()
        __validate(results)
    except ResultSetError as e:
        raise ResultSetError("%s in response from %s", (e.message, r.url))
    results = results["ResultSet"]
    if int(results["totalRecords"]) == 0:
        raise NullAccessionError("no accession id returned for %s" % label)
    projects = set()
    ids = set()
    experiments = []
    for item in results["Result"]:
        projects.add(item["project"])
        ids.add(item["ID"])
        label = item["label"]
        uri = item["URI"]
        if item["xnat:mrsessiondata/id"]:
            experiments.append((item["xnat:mrsessiondata/id"], item["xnat:mrsessiondata/label"]))
    if len(projects) > 1:
        raise MultipleAccessionError("too many projects returned for label %s" % label)
    if len(ids) > 1:
        raise MultipleAccessionError("too many ids returned for label %s" % label)
    return Subject(uri=uri, id=ids.pop(), project=projects.pop(),
                   label=label, experiments=experiments)

Experiment = col.namedtuple("Experiment", [
    "uri",
    "label",
    "id",
    "project",
    "subject_id",
    "subject_label",
    "archived_date"
])
'''
Container to hold XNAT Experiment information. Fields include the URI (uri), 
Accession ID (id), Project (project), Label (label), Subject Accession ID (subject_id),
Subject label (subject_label), and archived date (archived_date).
'''

def experiment(auth, label, project=None):
    '''
    Get the URI, Accession ID, Subject ID, and Project for any experiment 
    label. If label is tied to multiple Accession IDs, a Project must be 
    specified.
    
    Example:
        >>> import yaxil
        >>> auth = yaxil.XnatAuth(url='...', username='...', password='...')
        >>> yaxil.experiment(auth, 'AB1234C')
        Experiment(uri=u'/data/experiments/XNAT_E0001', label=u'AB1234C', id=u'XNAT_E0001', 
            project=u'MyProject', subject_id=u'XNAT_S0001', subject_label='ABC')

    :param auth: XNAT authentication
    :type auth: :mod:`yaxil.XnatAuth`
    :param label: XNAT Experiment label
    :type label: str
    :param project: XNAT Experiment Project
    :type project: str
    :returns: Experiment named tuple
    :rtype: Experiment
    '''
    if not label:
        raise AccessionError("label cannot be empty")
    url = "%s/data/experiments" % auth.url.rstrip('/')
    logger.debug("issuing http request %s", url)
    columns = [
        "ID",
        "label",
        "project",
        "xnat:subjectassessordata/subject_id",
        "subject_label",
        "insert_date"
    ]
    payload = {
        "label": label, 
        "columns": ",".join(columns)
    }
    if project:
        payload["project"] = project
    r = requests.get(url, params=payload, auth=(auth.username, auth.password), 
                     verify=CHECK_CERTIFICATE)
    if r.status_code != requests.codes.ok:
        raise AccessionError("response not ok (%s) from %s" % (r.status_code, r.url))
    try:
        results = r.json()
        __validate(results)
    except ResultSetError as e:
        raise ResultSetError("%s in response from %s", (e.message, r.url))
    results = results["ResultSet"]
    if int(results["totalRecords"]) == 0:
        raise NullAccessionError("no accession id returned for label %s" % label)
    elif int(results["totalRecords"]) > 1:
        raise MultipleAccessionError("too many accession ids returned " + \
            "for label %s" % label)
    if "ID" not in results["Result"][0]:
        raise AccessionError("id not in result from %s" % r.url)
    r = results["Result"][0]
    return Experiment(uri=r["URI"], id=r["ID"], project=r["project"],
                      label=r["label"], subject_id=r["subject_ID"], 
                      subject_label=r["subject_label"], 
                      archived_date=r["insert_date"])

def accession(auth, label, project=None):
    '''
    Get the Accession ID for any experiment label. If label is tied to 
    multiple Accession IDs, a Project must be supplied.

    Example:
        >>> import yaxil
        >>> auth = yaxil.XnatAuth(url='...', username='...', password='...')
        >>> yaxil.accession(auth, 'AB1234C')
        u'XNAT_E00001'

    :param auth: XNAT authentication
    :type auth: :mod:`yaxil.XnatAuth`
    :param label: XNAT Experiment label
    :type label: str
    :param project: XNAT Experiment Project
    :type project: str
    :returns: Accession ID
    :rtype: str
    '''
    return experiment(auth, label, project).id

def download(auth, accession, ids, out_dir='.', in_mem=True, progress=False, 
             attempts=1):
    '''
    Download scan data from XNAT.
    
    Example:
        >>> import yaxil
        >>> auth = yaxil.XnatAuth(url='...', username='...', password='...')
        >>> aid = yaxil.accession(auth, 'AB1234C')
        >>> yaxil.download(auth, aid, [1, 2], out_dir='./data')

    :param auth: XNAT authentication
    :type auth: :mod:`yaxil.XnatAuth`
    :param accession: XNAT Accession ID
    :type accession: str
    :param ids: Scan IDs (i.e., scan numbers)
    :type ids: list
    :param out_dir: Output directory
    :type out_dir: str
    :param in_mem: Keep downloaded file content in memory
    :type in_mem: bool
    :param progress: Show download progress every N bytes
    :type progress: int
    :param attempts: Number of download attempts
    :type retries: int
    ''' 
    # build the url
    if not ids:
        raise DownloadError("no scan ids supplied")
    url = "%s/data/experiments/%s/scans/%s/files?format=zip" % (auth.url.rstrip('/'), 
            accession, ','.join([str(x) for x in ids]))
    # issue the http request, with exponential backoff retry behavior
    backoff = 10
    for _ in range(attempts): 
        logger.debug("issuing http request %s", url)
        r = requests.get(url, stream=True, auth=(auth.username, auth.password), verify=CHECK_CERTIFICATE)
        logger.debug("response headers %s", r.headers)
        if r.status_code == requests.codes.ok:
            break
        fuzz = random.randint(0, 10)
        logger.warn("download unsuccessful (%s), retrying in %s seconds", r.status_code, 
                    backoff + fuzz)
        time.sleep(backoff + fuzz)
        backoff *= 2
    # if we still have a not-ok status at this point, the download failed
    if r.status_code != requests.codes.ok:
        raise DownloadError("response not ok (%s) from %s" % (r.status_code, r.url))
    # create output directory
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    # keep response content in memory or write to a file (memory is obviously faster, but limited)
    if in_mem:
        content = io.BytesIO()
        logger.debug("response content will be read into memory")
    else:
        content = tf.NamedTemporaryFile(dir=out_dir, prefix="xnat", suffix=".zip")
        logger.debug("response content will be stored on disk %s", content.name)
    # progress indicator setup
    if progress:
        sys.stdout.write("reading response data: ")
        sys.stdout.flush()
    # read response content in chunks
    meter = 0
    chunk_size = 1024
    for chunk in r.iter_content(chunk_size=chunk_size):
        if progress and meter >= progress:
            sys.stdout.write(next(commons.spinner)); sys.stdout.flush()
            sys.stdout.write('\b')
            meter = 0
        content.write(chunk)
        meter += chunk_size
    # progress indicator shut down
    if progress:
        sys.stdout.write('done.\n'); sys.stdout.flush()
    # load reponse content into a zipfile object
    try:
        zf = zipfile.ZipFile(content, allowZip64=True)
    except zipfile.BadZipfile:
        with tf.NamedTemporaryFile(dir=out_dir, prefix="xnat",
                                   suffix=".zip", delete=False) as fo:
            content.seek(0)
            fo.write(content.read())
        raise DownloadError("bad zip file, written to %s" % fo.name)
    # finally extract the zipfile (with various nasty edge cases handled)
    logger.debug("extracting zip archive to %s", out_dir)
    extract(zf, content, out_dir)

def extract(zf, content, out_dir='.'):
    '''
    Extracting a Java 1.6 XNAT ZIP archive in Python which is not as trivial as
    it first seems.

    :param zf: ZipFile object
    :type zf: zipfile.ZipFile
    :param out_dir: Output directory
    :type out_dir: str
    '''
    previous_header_offset = 0
    compensation = Namespace(value=2**32, factor=0)
    for i,member in enumerate(zf.infolist()):
        '''
        Right... so when Java 1.6 produces a Zip filesystem that exceeds 2^32
        bytes, the Central Directory local file header offsets after the 2^32 
        byte appear to overflow. The Python zipfile module then adds any 
        unexpected bytes to each header offset thereafter. This attempts to fix 
        that. My guess is that this comment might make perfect sense now, but 
        will make aboslutely no sense in about a year.
        '''
        # undo concat padding added from zipfile.py:819
        if i == 0:
            concat = member.header_offset
        member.header_offset -= concat
        # if a header offset moves backward, add 2^32 bytes * factor
        if previous_header_offset > member.header_offset:
            compensation.factor += 1
        previous_header_offset = member.header_offset
        member.header_offset += compensation.value * compensation.factor
        # read the archive member into a bytes file-like object
        try:
            bio = io.BytesIO(zf.read(member.filename))
        except zipfile.BadZipfile:
            with tf.NamedTemporaryFile(dir=out_dir, prefix="xnat",
                                   suffix=".zip", delete=False) as fo:
                content.seek(0)
                fo.write(content.read())
            raise DownloadError("bad zip file, written to %s" % fo.name)
        # xnat archives may contain files that are gzipped without the .gz
        if not member.filename.endswith(".gz"):
            try:
                gz = gzip.GzipFile(fileobj=bio, mode="rb")
                gz.read()
                bio = gz
            except IOError:
                pass
        # write the file out to the filesystem
        bio.seek(0)
        f = os.path.join(out_dir, os.path.basename(member.filename))
        with open(f, "wb") as fo:
            fo.write(bio.read())

def __validate(r, check=("ResultSet", "Result", "totalRecords")):
    '''
    Validate JSON result set.

    :param r: Result set data in JSON format
    :type r: dict
    :param check: Fields to check
    :type check: tuple
    :returns: Result set is valid
    :rtype: bool
    '''
    if "ResultSet" in check and "ResultSet" not in r:
        raise ResultSetError("no 'ResultSet'")
    if "Result" in check and "Result" not in r["ResultSet"]:
        raise ResultSetError("no 'Result' in 'ResultSet'")
    if "totalRecords" in check and "totalRecords" not in r["ResultSet"]:
        raise ResultSetError("no 'totalRecords' in 'ResultSet'")
    return True

def scansearch(auth, accession, filt):
    '''
    Search for scans by supplying a set of SQL-based conditionals.

    Example:
        >>> import yaxil
        >>> auth = yaxil.XnatAuth(url='...', username='...', password='...')
        >>> aid = yaxil.accession(auth, 'AB1234C')
        >>> query = {
        ...   'eor1': "note LIKE %EOR1%",
        ...   'eor2': "note LIKE %EOR2%",
        ...   'mpr':  "series_description='T1_MEMPRAGE RMS' OR note LIKE %ANAT%"
        ... }
        >>> yaxil.scansearch(auth, aid, query)
        {"mpr": [4], "eor1": [13], "eor2": [14]}

    :param auth: XNAT authentication
    :type auth: :mod:`yaxil.XnatAuth`
    :param accession: XNAT Accession ID
    :type accession: str
    :param filt: Scan filter
    :type filt: dict
    :returns: Dictionary of tokens and matching scan ids
    :rtype: dict
    '''
    # get scans for accession as a csv
    url = "%s/data/experiments/%s/scans?format=csv" % (auth.url.rstrip('/'), accession)
    logger.debug("issuing http request %s", url)
    r = requests.get(url, auth=(auth.username, auth.password), verify=CHECK_CERTIFICATE)
    if r.status_code != requests.codes.ok:
        raise ScanSearchError("response not ok (%s) from %s" % (r.status_code, r.url))
    if not r.content:
        raise ScanSearchError("response is empty from %s" % r.url)
    # read the result into a csv reader
    reader = csv.reader(io.StringIO(r.content.decode()))
    columns = next(reader)
    # create an in-memory database
    conn = sqlite3.connect(":memory:")
    c = conn.cursor()
    # create scans table and insert data
    c.execute("CREATE TABLE scans (%s)" % ','.join(columns))
    query = "INSERT INTO scans VALUES (%s)" % ','.join('?' * len(columns))
    for row in reader:
        c.execute(query, [commons.cast(x) for x in row])
    conn.commit()
    # run the user supplied filters and return result
    result = col.defaultdict(list)
    for token,filt in iter(filt.items()):
        try:
            result[token] = [x[0] for x in c.execute("SELECT ID FROM scans where %s" % filt)]
        except sqlite3.OperationalError:
            logger.critical("something is wrong with the filter: %s", filt)
            raise
    return result

def scans(auth, fmt, autobox=True, accession=None, project=None):
    '''
    Get scan information.
    
    Example:
        >>> import yaxil
        >>> from yaxil import Format
        >>> auth = yaxil.XnatAuth(url='...', username='...', password='...')
        >>> aid = yaxil.accession(auth, 'AB1234C')
        >>> csv = yaxil.scans(auth, Format.CSV, accession=aid)

    :param auth: XNAT authentication
    :type auth: :mod:`yaxil.XnatAuth`
    :param accession: XNAT Accession ID
    :type accession: str
    :param fmt: Saved search format
    :type fmt: :mod:`yaxil.Format`
    :param autobox: Autobox response data into a reader or data structure
    :type autobox: bool
    :param accession: XNAT Accession ID
    :type accession: str
    :param project: XNAT Project ID
    :type project: str
    :returns: Requested scan data
    :rtype: :mod:`dict` | :mod:`ElementTree` | :mod:`csv.reader`
    '''
    x = __patch1(scans, auth, project, fmt, "xnat:mrSessionData")
    if x:
        return x
    path = "/data/experiments"
    params = {
        "xsiType": "xnat:mrSessionData",
        "columns": ','.join(scans.columns.keys())
    }
    if project:
        params["project"] = project
    if accession:
        params["xnat:mrSessionData/ID"] = accession
    _,result = get(auth, path, fmt, autobox=autobox, params=params)
    return result
scans.columns = {
    "URI": "session_uri",
    "insert_date": "date_archived",
    "operator": "operator",
    "insert_user": "archiver",
    "xnat:mrsessiondata/id": "session_id",
    "xnat:mrsessiondata/label": "session_label",
    "xnat:mrsessiondata/project": "session_project",
    "xnat:mrsessiondata/date": "date_scanned",
    "xnat:mrsessiondata/time": "time_scanned",
    "xnat:subjectdata/id": "subject_id",
    "subject_label": "subject_label",
    "subject_project": "subject_project",
    "xnat:mrscandata/id": "id",
    "xnat:mrscandata/quality": "quality",
    "xnat:mrscandata/series_description": "series_description",
    "xnat:mrscandata/scanner": "scanner",
    "xnat:mrscandata/scanner/manufacturer": "scanner_manufacturer",
    "xnat:mrscandata/scanner/model": "scanner_model",
    "xnat:mrscandata/frames": "frames",
    "xnat:mrscandata/fieldStrength": "field_strength",
    "xnat:mrscandata/note": "note",
    "xnat:mrscandata/type": "type",
    "xnat:mrscandata/parameters/voxelres/x": "vox_x",
    "xnat:mrscandata/parameters/voxelres/y": "vox_y",
    "xnat:mrscandata/parameters/voxelres/z": "vox_z",
    "xnat:mrscandata/parameters/fov/x": "fov_x",
    "xnat:mrscandata/parameters/fov/y": "fov_y",
    "xnat:mrscandata/parameters/tr": "tr",
    "xnat:mrscandata/parameters/te": "te",
    "xnat:mrscandata/parameters/flip": "flip",
    "xnat:mrscandata/parameters/sequence": "sequence",
    "xnat:mrscandata/parameters/scantime": "time",
    "xnat:mrscandata/parameters/imagetype": "image_type",
    "xnat:mrscandata/parameters/scansequence": "scan_sequence",
    "xnat:mrscandata/parameters/seqvariant": "sequence_variant",
    "xnat:mrscandata/parameters/acqtype": "acquisition_type",
    "xnat:mrscandata/parameters/pixelbandwidth": "pix_bandwidth"
}

def extendedboldqc(auth, fmt, autobox=True, accession=None, project=None):
    '''
    Get ExtendedBOLDQC information

    Example:
        >>> import yaxil
        >>> from yaxil import Format
        >>> auth = yaxil.XnatAuth(url='...', username='...', password='...')
        >>> aid = yaxil.accession(auth, 'AB1234C')
        >>> js = yaxil.extendedboldqc(auth, Format.JSON, accession=aid)

    :param auth: XNAT authentication
    :type auth: :mod:`yaxil.XnatAuth`
    :param accession: XNAT Accession ID
    :type accession: str
    :param fmt: Saved search format
    :type fmt: :mod:`yaxil.Format`
    :param autobox: Autobox response data into a reader or data structure
    :type autobox: bool
    :param accession: XNAT Accession ID
    :type accession: str
    :param project: XNAT Project ID
    :type project: str
    :returns: Requested ExtendedBOLDQC data
    :rtype: :mod:`dict` | :mod:`ElementTree` | :mod:`csv.reader`
    '''
    x = __patch1(extqc, auth, project, fmt, "neuroinfo:extendedboldqc")
    if x:
        return x
    path = "/data/experiments"
    params = {
        "xsiType": "neuroinfo:extendedboldqc",
        "columns": ','.join(extendedboldqc.columns.keys())
    }
    if project:
        params["project"] = project
    if accession:
        params["xnat:mrSessionData/ID"] = accession
    _,result = get(auth, path, fmt, autobox=autobox, params=params)
    return result
extqc = extendedboldqc
extendedboldqc.columns = {
    "xnat:mrsessiondata/id": "session_id",
    "xnat:mrsessiondata/label": "session_label",
    "xnat:mrsessiondata/project": "project",
    "xnat:subjectdata/label": "subject_label",
    "xnat:subjectdata/id": "subject_id",
    "neuroinfo:extendedboldqc/id": "id",
    "neuroinfo:extendedboldqc/scan/scan_id": "scan_id",
    "neuroinfo:extendedboldqc/pipeline/status": "status",
    "neuroinfo:extendedboldqc/scan/n_vols": "nvols",
    "neuroinfo:extendedboldqc/scan/skip": "skip",
    "neuroinfo:extendedboldqc/scan/qc_thresh": "mask_threshold",
    "neuroinfo:extendedboldqc/scan/qc_nvox": "nvoxels",
    "neuroinfo:extendedboldqc/scan/qc_mean": "mean",
    "neuroinfo:extendedboldqc/scan/qc_max": "max",
    "neuroinfo:extendedboldqc/scan/qc_min": "min",
    "neuroinfo:extendedboldqc/scan/qc_stdev": "stdev",
    "neuroinfo:extendedboldqc/scan/qc_ssnr": "ssnr",
    "neuroinfo:extendedboldqc/scan/qc_vsnr": "vsnr",
    "neuroinfo:extendedboldqc/scan/qc_slope": "slope",
    "neuroinfo:extendedboldqc/scan/mot_n_tps": "mot_n_tps",
    "neuroinfo:extendedboldqc/scan/mot_rel_x_sd": "mot_rel_x_sd",
    "neuroinfo:extendedboldqc/scan/mot_rel_x_max": "mot_rel_x_max",
    "neuroinfo:extendedboldqc/scan/mot_rel_x_1mm": "mot_rel_x_1mm",
    "neuroinfo:extendedboldqc/scan/mot_rel_x_5mm": "mot_rel_x_5mm",
    "neuroinfo:extendedboldqc/scan/mot_rel_y_mean": "mot_rel_y_mean",
    "neuroinfo:extendedboldqc/scan/mot_rel_y_sd": "mot_rel_y_sd",
    "neuroinfo:extendedboldqc/scan/mot_rel_y_max": "mot_rel_y_max",
    "neuroinfo:extendedboldqc/scan/mot_rel_y_1mm": "mot_rel_y_1mm",
    "neuroinfo:extendedboldqc/scan/mot_rel_y_5mm": "mot_rel_y_5mm",
    "neuroinfo:extendedboldqc/scan/mot_rel_z_mean": "mot_rel_z_mean",
    "neuroinfo:extendedboldqc/scan/mot_rel_z_sd": "mot_rel_z_sd",
    "neuroinfo:extendedboldqc/scan/mot_rel_z_max": "mot_rel_z_max",
    "neuroinfo:extendedboldqc/scan/mot_rel_z_1mm": "mot_rel_z_1mm",
    "neuroinfo:extendedboldqc/scan/mot_rel_z_5mm": "mot_rel_z_5mm",
    "neuroinfo:extendedboldqc/scan/mot_rel_xyz_mean": "mot_rel_xyz_mean",
    "neuroinfo:extendedboldqc/scan/mot_rel_xyz_sd": "mot_rel_xyz_sd",
    "neuroinfo:extendedboldqc/scan/mot_rel_xyz_max": "mot_rel_xyz_max",
    "neuroinfo:extendedboldqc/scan/mot_rel_xyz_1mm": "mot_rel_xyz_1mm",
    "neuroinfo:extendedboldqc/scan/mot_rel_xyz_5mm": "mot_rel_xyz_5mm",
    "neuroinfo:extendedboldqc/scan/rot_rel_x_mean": "rot_rel_x_mean",
    "neuroinfo:extendedboldqc/scan/rot_rel_x_sd": "rot_rel_x_sd",
    "neuroinfo:extendedboldqc/scan/rot_rel_x_max": "rot_rel_x_max",
    "neuroinfo:extendedboldqc/scan/rot_rel_y_mean": "rot_rel_y_mean",
    "neuroinfo:extendedboldqc/scan/rot_rel_y_sd": "rot_rel_y_sd",
    "neuroinfo:extendedboldqc/scan/rot_rel_y_max": "rot_rel_y_max",
    "neuroinfo:extendedboldqc/scan/rot_rel_z_mean": "rot_rel_z_mean",
    "neuroinfo:extendedboldqc/scan/rot_rel_z_sd": "rot_rel_z_sd",
    "neuroinfo:extendedboldqc/scan/rot_rel_z_max": "rot_rel_z_max",
    "neuroinfo:extendedboldqc/scan/mot_abs_x_mean": "mot_abs_x_mean",
    "neuroinfo:extendedboldqc/scan/mot_abs_x_sd": "mot_abs_x_sd",
    "neuroinfo:extendedboldqc/scan/mot_abs_x_max": "mot_abs_x_max",
    "neuroinfo:extendedboldqc/scan/mot_abs_y_mean": "mot_abs_y_mean",
    "neuroinfo:extendedboldqc/scan/mot_abs_y_sd": "mot_abs_y_sd",
    "neuroinfo:extendedboldqc/scan/mot_abs_y_max": "mot_abs_y_max",
    "neuroinfo:extendedboldqc/scan/mot_abs_z_mean": "mot_abs_z_mean",
    "neuroinfo:extendedboldqc/scan/mot_abs_z_sd": "mot_abs_z_sd",
    "neuroinfo:extendedboldqc/scan/mot_abs_z_max": "mot_abs_z_max",
    "neuroinfo:extendedboldqc/scan/mot_abs_xyz_mean": "mot_abs_xyz_mean",
    "neuroinfo:extendedboldqc/scan/mot_abs_xyz_sd": "mot_abs_xyz_sd",
    "neuroinfo:extendedboldqc/scan/mot_abs_xyz_max": "mot_abs_xyz_max",
    "neuroinfo:extendedboldqc/scan/rot_abs_x_mean": "rot_abs_x_mean",
    "neuroinfo:extendedboldqc/scan/rot_abs_x_sd": "rot_abs_x_sd",
    "neuroinfo:extendedboldqc/scan/rot_abs_x_max": "rot_abs_x_max",
    "neuroinfo:extendedboldqc/scan/rot_abs_y_mean": "rot_abs_y_mean",
    "neuroinfo:extendedboldqc/scan/rot_abs_y_sd": "rot_abs_y_sd",
    "neuroinfo:extendedboldqc/scan/rot_abs_y_max": "rot_abs_y_max",
    "neuroinfo:extendedboldqc/scan/rot_abs_z_mean": "rot_abs_z_mean",
    "neuroinfo:extendedboldqc/scan/rot_abs_z_sd": "rot_abs_z_sd",
    "neuroinfo:extendedboldqc/scan/rot_abs_z_max": "rot_abs_z_max"
}

def get(auth, path, fmt, autobox=True, params=None):
    '''
    Issue a GET request to the XNAT REST API and autobox the response content.
    
    Example:
        >>> import yaxil
        >>> from yaxil import Format
        >>> auth = yaxil.XnatAuth(url='...', username='...', password='...')
        >>> yaxil.get(auth, '/data/experiments', Format.JSON)

    :param auth: XNAT authentication
    :type auth: :mod:`yaxil.XnatAuth`
    :param path: API URL path
    :type path: str
    :param fmt: API result format
    :type fmt: :mod:`yaxil.Format`
    :param params: Additional query parameters
    :type params: dict
    :param autobox: Autobox response content into an appropriate reader or other data structure
    :type autobox: bool
    :returns: Tuple of (URL, :mod:`dict` | :mod:`xml.etree.ElementTree` | :mod:`csv.reader` | :mod:`str`)
    :rtype: tuple
    '''
    if not params:
        params = {}
    url = "%s/%s" % (auth.url.rstrip('/'), path.lstrip('/'))
    params["format"] = fmt
    logger.debug("issuing http request %s", url)
    logger.debug("query parameters %s", params)
    r = requests.get(url, params=params, auth=(auth.username, auth.password), verify=CHECK_CERTIFICATE)
    if r.status_code != requests.codes.ok:
        raise RestApiError("response not ok (%s) from %s" % (r.status_code, r.url))
    if not r.content:
        raise RestApiError("response is empty from %s" % r.url)
    if autobox:
        return r.url,_autobox(r.text, fmt)
    else:
        return r.url,r.content

def _autobox(content, format):
    '''
    Autobox response content

    :param content: Response content
    :type content: str
    :returns: Autoboxed content
    :param format: Format to return
    :type format: `yaxil.Format`
    :rtype: dict|xml.etree.ElementTree.Element|csvreader
    '''
    if format == Format.JSON:
        return json.loads(content)
    elif format == Format.XML:
        return etree.fromstring(content)
    elif format == Format.CSV:
        try:
            return csv.reader(io.BytesIO(content))
        except TypeError:
            # as per https://docs.python.org/2/library/csv.html#examples
            def unicode_csv_reader(unicode_csv_data, dialect=csv.excel, **kwargs):
                # csv.py doesn't do Unicode; encode temporarily as UTF-8:
                csv_reader = csv.reader(utf_8_encoder(unicode_csv_data),
                                        dialect=dialect, **kwargs)
                for row in csv_reader:
                    # decode UTF-8 back to Unicode, cell by cell:
                    yield [unicode(cell, 'utf-8') for cell in row]
            def utf_8_encoder(unicode_csv_data):
                for line in unicode_csv_data:
                    yield line.encode('utf-8')
            return unicode_csv_reader(io.StringIO(content))
    else:
        raise AutoboxError("unknown autobox format %s" % format)

def has(auth, xsitype, project=None):
    '''
    Test if a Project contains any items of a particular xsi:type

    Example:
        >>> import yaxil
        >>> auth = yaxil.XnatAuth(url='...', username='...', password='...')
        >>> yaxil.has(auth, 'neuroinfo:extendedboldqc', project='MyProject')

    :param auth: XNAT authentication
    :type auth: :mod:`yaxil.XnatAuth`
    :param xsitype: XNAT xsi:type
    :param xsitype: str
    :param project: XNAT Project
    :type project: str
    :returns: True or False
    :rtype: bool
    '''
    path = "/data/experiments"
    params = {
        "xsiType": xsitype,
        "columns": 'ID'
    }
    if project:
        params["project"] = project
    url,result = get(auth, path, Format.JSON, autobox=True, params=params)
    try:
        __validate(result)
    except ResultSetError as e:
        raise ResultSetError("%s in response from %s" % (e.message, url))
    if int(result["ResultSet"]["totalRecords"]) == 0:
        return False
    return True

def __patch1(f, auth, project, fmt, xsitype):
    '''
    XNAT REST API patch #1
    '''
    if not has(auth, xsitype, project):
        if fmt == Format.CSV:
            return _autobox(','.join(f.columns.keys()), fmt)
    return None


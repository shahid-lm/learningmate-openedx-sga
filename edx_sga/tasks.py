"""celery async tasks"""

import hashlib
import logging
import os
import tempfile
import zipfile

from django.core.files.storage import default_storage
from celery import shared_task
from opaque_keys.edx.locator import BlockUsageLocator
from common.djangoapps.student.models import user_by_anonymous_id
from submissions import api as submissions_api

from edx_sga.constants import ITEM_TYPE
from edx_sga.utils import get_file_storage_path, is_finalized_submission

from django.core.mail import send_mail
# from edx_ace import ace
# from edx_ace.message import Message
from openedx.core.lib.courses import get_course_by_id
from common.djangoapps.student.models import CourseAccessRole
from lms.djangoapps.lms_xblock.serializers import StaffGradedSubmissionsSerializer
from opaque_keys.edx.keys import CourseKey
from django.contrib.auth.models import User
import json

log = logging.getLogger(__name__)


def _get_student_submissions(block_id, course_id, locator):
    """
    Returns valid submission file paths with the username of the student that submitted them.

    Args:
        course_id (unicode): edx course id
        block_id (unicode): edx block id
        locator (BlockUsageLocator): BlockUsageLocator for the sga module

    Returns:
        list(tuple): A list of 2-element tuples - (student username, submission file path)
    """

    def final_submissions(submissions):
        for submission in submissions:
            if is_finalized_submission(submission_data=submission):
                yield submission

    submissions = submissions_api.get_all_submissions(course_id, block_id, ITEM_TYPE)
    return [
        (
            user_by_anonymous_id(submission["student_id"]).username,
            get_file_storage_path(
                locator, submission["answer"]["sha1"], submission["answer"]["filename"]
            ),
        )
        for submission in final_submissions(submissions)
    ]


def _compress_student_submissions(zip_file_path, block_id, course_id, locator):
    """
    Creates a zip file of all student submissions for some course

    Args:
        destination_path (str): path (including name) of folder/file which we want to compress.
    """
    student_submissions = _get_student_submissions(block_id, course_id, locator)
    if not student_submissions:
        return

    log.info(
        "Compressing %d student submissions to path: %s ",
        len(student_submissions),
        zip_file_path,
    )
    # Build the zip file in memory using temporary file.
    with tempfile.TemporaryFile() as tmp:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zip_pointer:
            for student_username, submission_file_path in student_submissions:
                log.info(
                    "Creating zip file for student: %s, submission path: %s ",
                    student_username,
                    submission_file_path,
                )
                with default_storage.open(
                    submission_file_path, "rb"
                ) as destination_file:
                    filename_in_zip = f"{student_username}_{os.path.basename(submission_file_path)}"
                    zip_pointer.writestr(filename_in_zip, destination_file.read())
        # Reset file pointer
        tmp.seek(0)
        # Write the bytes of the in-memory zip file to an actual file
        log.info("Moving zip file from memory to storage at path: %s ", zip_file_path)
        default_storage.save(zip_file_path, tmp)


@shared_task
def zip_student_submissions(course_id, block_id, locator_unicode, username):
    """
    Task to download all submissions as zip file

    Args:
        course_id (unicode): edx course id
        block_id (unicode): edx block id
        locator_unicode (unicode): Unicode representing a BlockUsageLocator for the sga module
        username (unicode): user name of the staff user requesting the zip file
    """
    locator = BlockUsageLocator.from_string(locator_unicode)
    zip_file_path = get_zip_file_path(username, course_id, block_id, locator)
    log.info("Creating zip file for course: %s at path: %s", locator, zip_file_path)
    if default_storage.exists(zip_file_path):
        log.info("Deleting already-existing zip file at path: %s", zip_file_path)
        default_storage.delete(zip_file_path)
    _compress_student_submissions(zip_file_path, block_id, course_id, locator)


def get_zip_file_dir(locator):
    """
    Returns the relative directory path where we are saving the zipped submissions file.

    Args:
        locator (BlockUsageLocator): BlockUsageLocator for the sga module
    """
    return "{loc.org}/{loc.course}/{loc.block_type}_zipped".format(loc=locator)


def get_zip_file_name(username, course_id, block_id):
    """
    Returns the filename and extension of a submission zip file given a username and some
    information about the course.

    Args:
        username (unicode): staff user name
        course_id (unicode): edx course id
        block_id (unicode): edx block id
    """
    _id=hashlib.md5(block_id.encode("utf-8")).hexdigest()
    return f"{username}_submissions_{_id}_{course_id}.zip"


def get_zip_file_path(username, course_id, block_id, locator):
    """
    Returns the relative file path of a submission zip file given a username and some
    information about the course.

    Args:
        username (unicode): user name
        course_id (unicode): edx course id
        block_id (unicode): edx block id
        locator (BlockUsageLocator): BlockUsageLocator for the sga module
    """
    return os.path.join(
        get_zip_file_dir(locator), get_zip_file_name(username, course_id, block_id)
    )
    
@shared_task(bind=True, default_retry_delay=30, max_retries=2)
def send_email_to_instructor(self,course_id,from_address,message_payload,direct_link,submission_uuid):
    try:
        if course_id not in ['',None]:
            if message_payload['assignments']:
                current_submission = [submission for submission in message_payload[['assignments']] if submission['submission_id'] == submission_uuid][0]
                message='''There's a new submission\nStudent Username : {}\nFilename : {}\nSubmitted At : {}\nGo To All Submissions : {}
                        '''.format(current_submission.get('username',None),
                                   current_submission.get('filename',None),
                                   current_submission.get('timestamp',None),
                                   direct_link
                                )
            else:
                message = 'No submissions'
            mail_subject=f"New submission for Staff Graded Assignment"
            # filter CourseAccessRole model to get list of instructor Ids
            all_teacher_emailIds = []
            course_access_objs = CourseAccessRole.objects.filter(course_id=course_id).values()
            for course_obj in course_access_objs:
                to_email = User.objects.filter(id=course_obj['user_id']).values_list('email',flat=True)[0]
                all_teacher_emailIds.extend([to_email])
            send_mail(
                subject= mail_subject,
                message=message,
                from_email=from_address,
                recipient_list=all_teacher_emailIds,
                fail_silently=True,
            )
            log.info('################## Sent email to instructor ##################')
            return
        log.error('################## Couldn\'t send email to instructor - CourseId not found ##################')
        return
    except Exception as e:
        log.error(f'################## Couldn\'t send email to instructor {str(e)} ##################')
        return
    
@shared_task(bind=True)
def save_entry_to_openedxdb(self, course_id, message_payload, direct_link,submission_uuid):
    try:
        # convert course_key to name
        course_key = CourseKey.from_string(course_id)
        course = get_course_by_id(course_key, depth=None)
        course_name = course.display_name_with_default
        teacher_ids = json.dumps(list(CourseAccessRole.objects.filter(course_id=course_id).values_list('user_id',flat=True)))
        current_submission = [submission for submission in message_payload[['assignments']] if submission['submission_id'] == submission_uuid][0]
        data = {
            "course_name" : course_name,
            "assignment_name" : message_payload['display_name'],
            "direct_link" : direct_link,#"https://learningmate.com/",
            "teacher_id" : teacher_ids, # list of techer_ids stored as string
            "student_username" : current_submission.get('username',None)
        }
        data.update({"submission_id" : submission_uuid})
        log.info(f'############# save_entry_to_openedxdb data-> {data}#############')
        serializer = StaffGradedSubmissionsSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            log.info('############## save_entry_to_openedxdb - successfully saved sga entry')
        else:
            log.error('############## Couldn\'t save sga entry - serializer not valid')
    except Exception as e:
        log.error(f'############## Couldn\'t save sga entry -error {str(e)}')

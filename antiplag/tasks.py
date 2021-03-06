from math import ceil
import sys

from langdetect import detect
from celery import shared_task
from django.core.mail import send_mail
from django.utils.translation import ugettext as _

from .constants import EMAIL_SENDER
from .enums import SubmissionStatus, MatchType
from .models import Submission, Document
from nlp.elastic import Elastic
from nlp.text_comparison import text_comparison
from nlp.text_preprocessing import extract_text_from_file, preprocess_text
from django_project.settings import SIMILARITY_THRESHOLD


@shared_task(name="antiplag.tasks.process_documents")
def process_documents(submission_id):
    try:
        submission = Submission.objects.get(id=submission_id)
    except:
        return

    # update submission status
    submission.status = SubmissionStatus.PROCESSING
    submission.save()

    documents = submission.documents.all()

    for document in documents:
        try:
            # extract file contents
            if document.type == Document.DocumentType.FILE:
                document.text_raw = process_file(document.file)

            # preprocess text
            document.language = detect_language(document.text_raw)
            document.text = process_raw_text(document.text_raw, document.language)

            # save the document
            document.save()

        except Exception as e:
            print(e, file=sys.stderr)

            if document.type == Document.DocumentType.FILE:
                document.text_raw = ""
            document.text = document.text_raw
            document.save()

    # document comparison
    compare_documents(documents)

    # update submission status
    submission.status = SubmissionStatus.PROCESSED
    submission.save()

    # send email when done
    if submission.email is not None:
        send_mail(
            _("Antiplag - Your check has finished!"),
            _("Check the results of your check at https://antiplag.sk/submission/%s/")
            % submission.id,
            EMAIL_SENDER,
            [submission.email],
            fail_silently=False,
        )


def process_file(file):
    return extract_text_from_file(file.path)


def detect_language(text_raw):
    return detect(text_raw)


def process_raw_text(text, language):
    return preprocess_text(
        text,
        language=language,
        words_to_numbers=True,
        remove_numbers=False,
        tokenize_words=False,
        lemmatize=False,
        remove_stopwords=True,
    )[1]


def compare_documents(
    documents, threshold=SIMILARITY_THRESHOLD, similar_count=10, round_decimal_places=2
):
    """
    Compare given documents against each other and N most similar documents from elastic
    """
    round_factor = 10 ** round_decimal_places

    for doc in documents:
        # returns list of dictionaries
        # {
        #   "document_name": "referaty-zemegula"
        #   "text": "Co je zemegula? Je to hoax, zem je predsa plocha."
        # }
        similar_documents = Elastic.find_similar(doc.text, similar_count)

        # make new list and remove current doc
        user_documents = list(documents)
        user_documents.remove(doc)

        result_similarity = 0
        compared_count = 0

        # Compare current document with elastic docs
        for similar_doc in similar_documents:

            try:
                # returns percentage representing how similar docs are
                similarity = text_comparison(doc.text, similar_doc["text_preprocessed"])
                result_similarity += similarity["first_to_second"]["similarity"]
            except Exception:
                # TODO: Should uncomparable documents be included?
                continue

            if similarity["first_to_second"]["similarity"] > threshold:
                compared_count += 1
                doc.results.create(
                    match_type=MatchType.CORPUS,
                    match_id=similar_doc["elastic_id"],
                    match_name=similar_doc["name"],
                    percentage=ceil(
                        similarity["first_to_second"]["similarity"] * round_factor
                    )
                    / round_factor,
                    ranges=similarity["first_to_second"]["intervals"],
                )

        # Compare current document against user uploaded docs
        for user_doc in user_documents:
            try:
                # returns percentage representing how similar docs are
                similarity = text_comparison(doc.text, user_doc.text)
                result_similarity += similarity["first_to_second"]["similarity"]
            except Exception:
                # TODO: Should uncomparable documents be included?
                continue

            if similarity["first_to_second"]["similarity"] > threshold:
                compared_count += 1

                doc.results.create(
                    match_type=MatchType.UPLOADED,
                    match_id=user_doc.id,
                    match_name=user_doc.name,
                    percentage=ceil(
                        similarity["first_to_second"]["similarity"] * round_factor
                    )
                    / round_factor,
                    ranges=similarity["first_to_second"]["intervals"],
                )

        if compared_count > 0:
            result_similarity /= compared_count

        # save total percentage to the doc
        doc.total_percentage = ceil(result_similarity * round_factor) / round_factor
        doc.save()

"""ORM models.

Importing every model here ensures they are registered on ``Base.metadata`` so that
Alembic autogenerate can see them.
"""

from app.models.activity_log import ActivityLog
from app.models.advisor_availability import (
    AdvisorAvailabilityOverride,
    AdvisorWeeklySlot,
)
from app.models.advisor_bookmark import AdvisorBookmark
from app.models.advisor_credential import AdvisorCredential
from app.models.advisor_lead import AdvisorLead
from app.models.advisor_matching_weights import AdvisorMatchingWeights
from app.models.advisor_profile import (
    AdvisorCountryExpertise,
    AdvisorLanguage,
    AdvisorOfferedService,
    AdvisorProfile,
    AdvisorService,
    AdvisorVisaSpecialization,
)
from app.models.assessment import (
    Assessment,
    AssessmentAnswer,
    AssessmentCategoryScore,
    AssessmentInsight,
    AssessmentQuestion,
    AssessmentQuestionOption,
    AssessmentTip,
)
from app.models.assessment_ab_variant import AssessmentAbVariant
from app.models.assessment_threshold import AssessmentThreshold
from app.models.booking import Booking
from app.models.booking_document_request import BookingDocumentRequest
from app.models.booking_note import BookingNote, BookingNoteAttachment
from app.models.conversation import Conversation
from app.models.eligibility_rule import EligibilityRule
from app.models.message import Message, MessageAttachment
from app.models.payout_request import PayoutRequest
from app.models.review import Review
from app.models.seeker_document import SeekerDocument, SeekerDocumentComment
from app.models.seeker_profile import (
    SeekerCountryVisited,
    SeekerPriorVisa,
    SeekerProfile,
)
from app.models.support_ticket import SupportTicket
from app.models.ticket_message import TicketMessage, TicketMessageAttachment
from app.models.token import RefreshToken, UserToken
from app.models.transaction import Transaction
from app.models.transaction_event import TransactionEvent
from app.models.user import User
from app.models.visa_type import VisaType

__all__ = [
    "User",
    "VisaType",
    "ActivityLog",
    "RefreshToken",
    "UserToken",
    "SeekerProfile",
    "SeekerCountryVisited",
    "SeekerPriorVisa",
    "AdvisorProfile",
    "AdvisorVisaSpecialization",
    "AdvisorCountryExpertise",
    "AdvisorLanguage",
    "AdvisorOfferedService",
    "AdvisorService",
    "AdvisorCredential",
    "AdvisorBookmark",
    "AdvisorLead",
    "AdvisorMatchingWeights",
    "Assessment",
    "AssessmentAnswer",
    "AssessmentCategoryScore",
    "AssessmentInsight",
    "AssessmentQuestion",
    "AssessmentQuestionOption",
    "AssessmentTip",
    "AssessmentAbVariant",
    "AssessmentThreshold",
    "AdvisorWeeklySlot",
    "AdvisorAvailabilityOverride",
    "Booking",
    "BookingNote",
    "BookingNoteAttachment",
    "BookingDocumentRequest",
    "SeekerDocument",
    "SeekerDocumentComment",
    "EligibilityRule",
    "Review",
    "Conversation",
    "Message",
    "MessageAttachment",
    "Transaction",
    "TransactionEvent",
    "PayoutRequest",
    "SupportTicket",
    "TicketMessage",
    "TicketMessageAttachment",
]

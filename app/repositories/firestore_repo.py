import uuid

from google.cloud import firestore


class FirestoreRepository:
    def __init__(self, client, conv_coll, msg_subcoll):
        self.client = client
        self.conv_coll = conv_coll
        self.msg_subcoll = msg_subcoll

    def _conv_ref(self, conversation_id: str):
        return self.client.collection(self.conv_coll).document(conversation_id)

    def _msg_ref(self, conversation_id: str, message_id: str):
        return self._conv_ref(conversation_id).collection(self.msg_subcoll).document(message_id)

    def ensure_conversation(self, conversation_id: str, session_id: str):
        ref = self._conv_ref(conversation_id)
        snap = ref.get()
        if snap.exists:
            return snap, True

        data = {
            "conversation_id": conversation_id,
            "user_id": conversation_id,
            "session_id": session_id,
            "status": "bot",
            "handoff_active": False,
            "assignee": None,
            "assignee_name": None,
            "last_message_text": "",
            "last_in_from": None,
            "unread_for_assignee": 0,
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
            "lock_version": 0,
        }
        ref.set(data)
        return ref.get(), False

    def add_message_if_new(
        self,
        conversation_id: str,
        message_id: str,
        direction: str,
        by: str,
        text: str,
        media_url: str = None,
        media_type: str = None,
    ) -> bool:
        if not message_id:
            message_id = str(uuid.uuid4())
        ref = self._msg_ref(conversation_id, message_id)
        if ref.get().exists:
            return False

        msg_data = {
            "message_id": message_id,
            "direction": direction,
            "by": by,
            "text": text,
            "ts": firestore.SERVER_TIMESTAMP,
        }

        if media_url:
            msg_data["media_url"] = media_url
        if media_type:
            msg_data["media_type"] = media_type

        ref.set(msg_data)
        return True

    def update_conversation(self, conversation_id: str, **fields):
        fields["updated_at"] = firestore.SERVER_TIMESTAMP
        self._conv_ref(conversation_id).set(fields, merge=True)

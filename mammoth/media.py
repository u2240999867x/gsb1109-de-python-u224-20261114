import hashlib
import io

import cobble


@cobble.data
class Media(object):
    digest = cobble.field()
    data = cobble.field()

    def open(self):
        return io.BytesIO(self.data)


@cobble.data
class MediaReference(object):
    source = cobble.field()
    content_type = cobble.field()
    alt_text = cobble.field()
    media = cobble.field()
    external_uri = cobble.field()

    @property
    def is_external(self):
        return self.media is None


class MediaStore(object):
    def __init__(self):
        self._media_by_digest = {}
        self._references = []

    def add_internal(self, source, content_type, alt_text, data):
        digest = hashlib.sha256(data).hexdigest()
        media = self._media_by_digest.get(digest)
        if media is None:
            media = Media(digest=digest, data=data)
            self._media_by_digest[digest] = media

        return self._add_reference(
            source=source,
            content_type=content_type,
            alt_text=alt_text,
            media=media,
            external_uri=None,
        )

    def add_external(self, source, content_type, alt_text, uri):
        return self._add_reference(
            source=source,
            content_type=content_type,
            alt_text=alt_text,
            media=None,
            external_uri=uri,
        )

    def _add_reference(self, **kwargs):
        reference = MediaReference(**kwargs)
        self._references.append(reference)
        return reference

    @property
    def references(self):
        return list(self._references)

    @property
    def media(self):
        return list(self._media_by_digest.values())

    def find_by_alt_text(self, alt_text):
        return [
            reference
            for reference in self._references
            if reference.alt_text == alt_text
        ]

class AISBaseException(Exception):
    """The base exception for all exceptions"""


class InvalidNMEAMessageException(AISBaseException):
    """Invalid NMEA Message"""
    pass


class InvalidNMEAChecksum(AISBaseException):
    """Invalid Checksum for the NMEA message"""


class UnknownMessageException(AISBaseException):
    """Message not supported yet"""
    pass


class MissingMultipartMessageException(AISBaseException):
    """Multipart message with missing parts provided"""


class TooManyMessagesException(AISBaseException):
    """Too many messages"""


class UnknownPartNoException(AISBaseException):
    """Unknown part number"""


class InvalidDataTypeException(AISBaseException):
    """An Unknown data type was passed to an encoding/decoding function"""


class NonPrintableCharacterException(AISBaseException):
    """Not used anymore. But remains for backwards compatibility."""


class TagBlockNotInitializedException(Exception):
    """The TagBlock is not initialized"""


class MissingPayloadException(AISBaseException):
    """Valid NMEA Message without payload"""
    pass


class AnomalousNameExtensionException(AISBaseException):
    """A type 21 AtoN name extension is present although the 20-character
    main name was terminated early by an '@' character.

    The extension must only be interpreted when all 20 characters of the
    main name are used, so such an extension is anomalous and must not be
    appended to the name."""
    pass

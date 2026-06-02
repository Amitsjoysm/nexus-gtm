from nexus.ingestion.crm import CRMConnector, HubSpotConnector, SalesforceConnector
from nexus.ingestion.service import IngestionService, RawSignal, get_ingestion_service
from nexus.ingestion.sources import (
    SIGNAL_LIBRARY,
    DemoSignalSource,
    SignalSource,
    WebNewsSource,
)

__all__ = [
    "CRMConnector",
    "HubSpotConnector",
    "SalesforceConnector",
    "IngestionService",
    "RawSignal",
    "get_ingestion_service",
    "SIGNAL_LIBRARY",
    "DemoSignalSource",
    "SignalSource",
    "WebNewsSource",
]

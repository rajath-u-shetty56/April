class IngestionError(Exception):
    def __init__(self, code, field=None, status_code=400):
        self.code = code
        self.field = field
        self.status_code = status_code
        super().__init__(code)

    def result(self, event_uuid=None):
        return {"uuid": event_uuid, "status": "rejected", "code": self.code, "field": self.field}

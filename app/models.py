from dataclasses import dataclass, asdict

# (attribute, Excel header): order = column order
HEADERS = [
    ("university_name", "University Name"),
    ("geo", "GEO"),
    ("country", "Country/Region"),
    ("discipline", "Discipline Searched"),
    ("college_website", "College Website"),
    ("teaching_requested_field", "Teaching Requested Field?"),
    ("relevant_course", "Relevant Course"),
    ("target_department", "Target Department"),
    ("department_url", "Department URL"),
    ("contact_type", "Contact Type"),
    ("contact_name", "Contact Name"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("office", "Office"),
    ("profile_url", "Profile URL"),
    ("source_url", "Source URL"),
    ("research_area", "Research Area"),
    ("extraction_method", "Extraction Method"),
    ("name_confidence", "Name Confidence"),
    ("email_binding_confidence", "Email Binding Confidence"),
    ("department_match_confidence", "Department Match Confidence"),
    ("role_confidence", "Role Confidence"),
    ("status", "Status"),
]


@dataclass
class Result:
    """Result row for university faculty search.

    Status values:
    - "Success": Valid department and faculty contact extracted
    - "Website Not Found": Official website could not be resolved
    - "Field Not Found": No matching department page found
    - "Ambiguous Department": Multiple campus candidates found without disambiguating geo signal
    - "Department Page Blocked": Department URL discovered but page fetch blocked/forbidden
    - "Contact Not Found": Department found but no faculty contact could be extracted
    - "Error": Unexpected exception during pipeline execution
    """
    university_name: str = ""
    geo: str = ""
    country: str = ""
    discipline: str = ""
    college_website: str = ""
    teaching_requested_field: str = "UNKNOWN"
    relevant_course: str = ""
    target_department: str = ""
    department_url: str = ""
    contact_type: str = ""
    contact_name: str = "Not Found"
    email: str = ""
    phone: str = ""
    office: str = ""
    profile_url: str = ""
    source_url: str = ""
    research_area: str = ""
    extraction_method: str = ""
    name_confidence: float = 0.0
    email_binding_confidence: float = 0.0
    department_match_confidence: float = 0.0
    role_confidence: float = 0.0
    status: str = "Error"
    error: str = ""  # kept in SQLite/logs, not exported
    debug_file: str = ""  # kept in SQLite/logs, not exported

    def to_row(self) -> dict:
        return {header: getattr(self, attr) for attr, header in HEADERS}

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Contact:
    name: str
    role: str
    rank: int               # 0=HOD 1=Chair 2=Professor 3=Assoc 4=Asst 5=Other
    method: str             # HTML_TABLE / PDF_TABLE / STRUCTURED_HTML / DETERMINISTIC / LLM
    source_url: str = ""
    role_method: str = ""   # method that produced the role (may differ after merging)
    email: str = ""
    phone: str = ""
    office: str = ""
    profile_url: str = ""
    research_area: str = ""

    def __post_init__(self):
        self.role_method = self.role_method or self.method

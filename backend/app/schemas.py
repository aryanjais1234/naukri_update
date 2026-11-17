from pydantic import BaseModel, ConfigDict

class ProfileIn(BaseModel):
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    experience_years: str = ""
    current_employer: str = ""
    current_role: str = ""
    skills: str = ""
    preferred_roles: str = ""
    preferred_locations: str = ""
    expected_compensation: str = ""
    notice_period: str = ""
    resume_path: str = ""
    application_preferences: str = "{}"
    resume_filename: str = ""

class ProfileOut(ProfileIn):
    id: int
    model_config = ConfigDict(from_attributes=True)

class OpenUrlIn(BaseModel):
    url: str

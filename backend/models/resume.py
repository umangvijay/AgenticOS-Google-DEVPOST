from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import datetime, timezone

class ContactInfo(BaseModel):
    name: str
    email: str
    phone: Optional[str] = None
    linkedin: Optional[str] = None
    github: Optional[str] = None
    location: Optional[str] = None

class Experience(BaseModel):
    company: str
    title: str
    location: Optional[str] = None
    start_date: str
    end_date: Optional[str] = None # "Present" or date
    bullets: List[str]

class Education(BaseModel):
    institution: str
    degree: str
    graduation_date: Optional[str] = None

class Project(BaseModel):
    name: str
    description: Optional[str] = None
    bullets: List[str]
    technologies: List[str] = Field(default_factory=list)

class Resume(BaseModel):
    id: str = Field(description="Unique identifier of the resume")
    contact: ContactInfo
    summary: Optional[str] = None
    skills: List[str] = Field(default_factory=list)
    experience: List[Experience] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)
    projects: List[Project] = Field(default_factory=list)
    
class TailoredResumeVersion(BaseModel):
    id: str = Field(description="Unique identifier for the tailored version")
    source_resume_id: str = Field(description="ID of the immutable master resume")
    target_job_id: str = Field(description="ID of the job description this was tailored for")
    resume: Resume
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class JobDescription(BaseModel):
    raw_text: str
    title: str
    company: str
    location: Optional[str] = None
    seniority: Optional[str] = None
    required_skills: List[str] = Field(default_factory=list)
    preferred_skills: List[str] = Field(default_factory=list)
    responsibilities: List[str] = Field(default_factory=list)
    required_experience_years: Optional[int] = None
    education_requirements: List[str] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    extraction_version: str = "1.0"

class ATSScore(BaseModel):
    overall_score: float = Field(description="Score from 0.0 to 100.0")
    # Audit G08: this number is Sandtable's matching heuristic — weighted
    # skill/keyword/experience/education/completeness arithmetic — NOT a
    # guarantee of acceptance by any applicant-tracking system.
    score_kind: str = "heuristic"
    score_note: str = ("AgentOS résumé-match heuristic (§17.16): weighted "
                       "keyword/skill arithmetic — NOT a hiring probability, "
                       "NOT a third-party ATS certification, and NOT evidence "
                       "of factual correctness.")
    rubric_version: str = "agentos-match-heuristic-2.0"
    rubric_weights: Dict[str, float] = {
        "skill_match": 0.35, "keyword_match": 0.25,
        "experience_match": 0.2, "education_match": 0.1,
        "completeness": 0.1}
    matched_criteria: List[str] = Field(default_factory=list)
    missing_criteria: List[str] = Field(default_factory=list)
    inputs_considered: List[str] = Field(default_factory=list)
    skill_match_score: float
    keyword_match_score: float
    experience_match_score: float
    education_match_score: float
    completeness_score: float
    
    missing_required_skills: List[str] = Field(default_factory=list)
    missing_preferred_skills: List[str] = Field(default_factory=list)
    matched_keywords: List[str] = Field(default_factory=list)

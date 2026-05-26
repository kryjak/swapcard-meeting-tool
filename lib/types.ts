export type Attendee = {
  id: string;
  firstName: string;
  lastName: string;
  company: string;
  jobTitle: string;
  careerStage: string;
  biography: string;
  expertise: string[];
  interests: string[];
  helpOthers: string;
  needHelp: string;
  country: string;
  seekingWork: string;
  recruitment: string[];
  swapcardUrl: string;
  linkedinUrl: string;
};

export type Candidate = Attendee & {
  matchedWanted: string[];
  matchedOffered: string[];
  matchedLateral: string[];
  retrievalScore: number;
};

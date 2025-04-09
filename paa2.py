import requests
import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from typing import TypedDict, NamedTuple


class ProviderInfo(TypedDict):
    provider_npi: str
    provider_fname: str
    provider_lname: str
    provider_gender: str
    provider_degree: str
    provider_speciality: str
    provider_healow_uri: str
    accept_new_patients: str
    facility_id: str


class FacilityDateTimeSlot(NamedTuple):
    facility_id: str
    date: str
    time: str


facility_ids = {
    '1': 'Springfield - 6355 Walker Lane',
    '13': 'Potomac Yard - 3600 S. Glebe Rd',
    '20': 'Duke St - 2747 Duke St',
}


visit_type = {
    'sick': '188344',
    'well': '43397',
}


def payload_to_bytes(payload: dict) -> bytes:
    """
    Converts payload dict to bytes. 

    The requests package url-safe encodes the payload data to JSON
    if a dict is passed in.  This pre-converts it to bytes, which
    the API endpoints likes more.
    """
    return '&'.join(f'{k}={v}' for k, v in payload.items()).encode()


def bin_time(t: str, minutes: int=15):
    """
    Bins the time into `minutes` intervals.  Drops the seconds.
    I.e. "12:40:00" -> "12:30" when minutes=15
    """
    h, m, *_ = t.split(':')
    m_bin = (int(m) // minutes) * minutes
    return f'{h}:{m_bin:0>2}'


class PAAScheduleRetriever:
    HOST_ACTION = 'https://healow.com/apps/HealowWebController?action'
    PROVIDER_LIST_URL = f'{HOST_ACTION}=GetAvilableApptProvidersList'
    PROVIDER_SLOTS_URL = f'{HOST_ACTION}=GetProviderSlotsByDate'

    def __init__(self):
        self.sess = requests.Session()
        self.prepare_api_session()

    def _get_api_token(self) -> str:
        """
        Gets an API token and sets the cookies.
        """
        url = (
            'https://healow.com/apps/practice/'
            'pediatric-associates-of-alexandria-inc-3187?v=1'
        )
        res = self.sess.get(url)
        res.raise_for_status()
        m = re.search(
            r'name="_csrf"\s+content="([\w+\-]+)"',
            res.content.decode()
        )
        if not m:
            raise ValueError('Could not match "_csrf" to find token.')
        return m.groups()[0]


    def prepare_api_session(self) -> None:
        """
        Return a session object that is ready for API calls.
        """
        token = self._get_api_token()
        headers = {
            "Accept": "application/json,*/*;q=0.5",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-CSRF-TOKEN": token,
        }
        self.sess.headers.update(headers)

    def get_providers(self) -> list[ProviderInfo]:
        """
        Returns a list of provider info.
        """

        payload_base = {
            "apu_id": "9296",
            "speciality_name": "Pediatrician",
            "speciality_id": "10",
            "provider_npi": "",
            "prov_gender": "any",
            "language_id": "",
            "language": "Select",
            "lat": "",
            "lng": "",
            "sort_by": "appt_date",
            "sort_order": "ASC",
            "oa_source": "3",
            "page": "1",
            "limit": 100,
        }
        extra_1 = {
            "facility_id": '1',
            "zip": "22310",
            "location": "Alexandria,+VA",
        }
        extra_13 = {
            "facility_id": '13',
            "zip": "22202",
            "location": "Arlington,+VA",
        }
        extra_20 = {
            "facility_id": '20',
            "zip": "22314",
            "location": "Alexandria,+VA",
        }

        provider_keys = [
            'provider_npi',
            'provider_fname',
            'provider_lname',
            'provider_gender',
            'provider_degree',
            'provider_speciality',
            'provider_healow_uri',
            'accept_new_patients',
        ]
        provider_infos = []

        for extra in [extra_1, extra_13, extra_20]:
            p = payload_base.copy()
            p.update(extra)
            res = self.sess.post(self.PROVIDER_LIST_URL, data=payload_to_bytes(p))
            j = res.json()
            facility_id = extra['facility_id']
            providers: list[dict] = j['response']['prov_list']
            for prov_dict in providers:
                prov_info = {k: prov_dict.get(k) for k in provider_keys}
                prov_info['facility_id'] = extra['facility_id']
                provider_infos.append(prov_info)
        return provider_infos

    def _get_provider_slots_for_date(
        self, 
        provider: ProviderInfo, 
        date: str,
        ) -> list[FacilityDateTimeSlot]:
        """
        Gets slots for a specific day for a provider.
        """
        payload = {
            "npi": provider['provider_npi'],
            "apu_id": "9296",
            "facility_id": provider['facility_id'],
            "appt_date": date,
            "start_time": "06:00:00",
            "end_time": "23:59:00",
            "visit_type": "1",
            "visit_code": "SICK",
            "practice_visit_reason_id": "188344"
        }
        more = True
        time_slots: list[FacilityDateTimeSlot] = []
        while more:
            res = self.sess.post(
                self.PROVIDER_SLOTS_URL, 
                data=payload_to_bytes(payload)
            )
            j = res.json()
            if not j['status'] == 'success':
                break
            appts_info = j['response']['appt_more_slots']
            more = appts_info['more']
            if more:
                payload['start_time'] = appts_info['next_start_time']
            appts = appts_info.get('appt_slots', [])
            for appt in appts:
                time_slots.append(
                    FacilityDateTimeSlot(provider['facility_id'], appt['date'], bin_time(appt['time']))
                )
        return time_slots

    def _get_provider_slots(
        self, 
        provider: ProviderInfo, 
        n_days: int=3,
        ) -> list[FacilityDateTimeSlot]:
        """
        Gets slots for n days.
        """
        today = dt.date.today()
        days = [
            (today + dt.timedelta(days=i)).strftime('%Y-%m-%d')
            for i in range(n_days)
        ]
        slots = []
        for day in days:
            slots.extend(self._get_provider_slots_for_date(provider, day))
        return slots

    def get_all_available_times(self, n_days: int=3):
        providers = self.get_providers()
        executor = ThreadPoolExecutor(max_workers=8)
        futures: dict[Future, ProviderInfo] = {}
        slots_dict: dict[FacilityDateTimeSlot, list[ProviderInfo]] = {}
        for provider in providers:
            f = executor.submit(
                self._get_provider_slots,
                provider, 
                n_days
            )
            futures[f] = provider
        for i, f in enumerate(as_completed(futures), 1):
            print(f'\r Getting schedules: {i} / {len(futures)}', end='')
            slots = f.result()
            provider = futures[f]
            for slot in slots:
                slots_dict.setdefault(slot, []).append(provider)
        return slots_dict

        

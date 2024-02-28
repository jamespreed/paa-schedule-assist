from concurrent.futures import as_completed, ThreadPoolExecutor
from collections import defaultdict
from dataclasses import dataclass
import datetime as dt
from enum import Enum
from jinja2 import Template
from lxml import html
import requests
from tqdm import tqdm


class FacilityId(Enum):
    WalkerLaneSpringfield = 1
    GlebeRoadArlington = 13


class VisitType(Enum):
    sick = '43396'
    well = '43397'


@dataclass
class ProviderInfo:
    name: str
    facility: FacilityId
    degree: str
    npi: str
    time_slots: dict[str, list[str]]
    next_slot: dict[str, str]


class APIMap:
    host = 'https://healow.com/apps'
    practice_page = f'{host}/practice/pediatric-associates-of-alexandria-inc-3187?v=1'
    provider_list = f'{host}/HealowWebController?action=GetAvilableApptProvidersList'
    provider_details = f'{host}/HealowWebController?action=GetProviderSlotsAtFacility'
    provider_slots = f'{host}/HealowWebController?action=GetProviderSlotsByDate'


def payload_to_bytes(payload: dict) -> bytes:
    """
    Converts payload dict to bytes. 

    The requests package url-safe encodes the payload data to JSON
    if a dict is passed in.  This pre-converts it to bytes, which
    the API endpoints likes more.
    """
    return '&'.join(f'{k}={v}' for k, v in payload.items()).encode()


class PAAScheduleNabber:
    def __init__(self, visit_type: VisitType = VisitType.sick, workers=8) -> None:
        self.sess = requests.Session()
        self.workers = workers
        self.errors = []
        self.provider_list_payload = {
            "apu_id": "9296",
            "speciality_name": "Pediatrician",
            "speciality_id": "10",
            "facility_id": "13",
            "provider_npi": "",
            "prov_gender": "any",
            "language_id": "",
            "language": "Select",
            "zip": "22202",
            "lat": "",
            "lng": "",
            "location": "Arlington,+VA",
            "sort_by": "appt_date",
            "sort_order": "ASC",
            "oa_source": "3",
            "page": "1",
            "limit": 50,
        }
        self.provider_details_payload = {
            "oa_source": "3",
            "provider_npi": "",
            "apu_id": "9296",
            "facility_id": "13",
            "start_date": dt.date.today().isoformat(),
            "time_pref": "anytime",
            "is_pt_existing": "0",
            "practice_visit_reason_id": visit_type.value,
            "days": "5",
            "user_timezone_name": "America/New_York"
        }
        self.provider_more_times_payload = {
            "npi": "",
            "apu_id": "9296",
            "facility_id": "13",
            "appt_date": "",
            "start_time": "",
            "end_time": "23:59:00",
            "visit_type": "1",
            "visit_code": visit_type.name.upper(),
            "practice_visit_reason_id": "43396"
        }

    def __call__(self) -> list[ProviderInfo]:
        self._stage_1_get_token()
        print('--- Getting list of healthcare provider basic info ---')
        info_list = self._stage_2_get_provider_info()
        print('--- Retrieving daily schedule previews ---')
        prov_info_list = self._stage_3_get_provider_initial_schedule(info_list)
        print('--- Requesting addition available time slots for each day ---')
        prov_info_filled_list = self._stage_4_fill_more_times(prov_info_list)
        return prov_info_filled_list
    
    def update_session_headers(self, x_csrf_token: str) -> None:
        """
        After getting the X-CSRF-TOKEN from step 1, this sets the headers for
        use in the rest of the script.
        """
        headers = {
            "Accept": "application/json,*/*;q=0.5",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-CSRF-TOKEN": x_csrf_token,
        }
        self.sess.headers.update(headers)

    def _check_for_token(self):
        if not "X-CSRF-TOKEN" in self.sess.headers:
            raise KeyError('X-CSRF-TOKEN must be added to the session header.')

    @staticmethod
    def _parse_token(content: bytes) -> str:
        """
        Parses the content of the stage 1 response and returns the token.
        """
        h = html.fromstring(content)
        token = h.head.xpath('meta[@name="_csrf"]')[0].get('content')
        return token

    def _stage_1_get_token(self) -> str:
        """
        First stage: 
        - load the landing page for PAA.
        - parse the page to get the session token for the API. 
        """
        res = self.sess.get(APIMap.practice_page)
        token = self._parse_token(res.content)
        self.update_session_headers(token)
        return token

    @staticmethod
    def _extract_provider_info(res_json: dict) -> list[dict]:
        """
        Extracts the NPIs from the stage 2 JSON.
        """
        provider_dicts = res_json['response']['prov_list']
        return [
            {
                'npi': p['provider_npi'],
                'name': f"{p['provider_fname']} {p['provider_lname']}",
                'degree': p['provider_degree']
            }
            for p in provider_dicts
        ]

    def _stage_2_get_provider_info(self) -> list[str]:
        """
        Second stage: 
        - API call to list all providers (doctors/nurses) at a specific location.
        - extract the NPI for each healthcare provider.
        
        NPI is a unique ID for each healthcare professional.
        """
        self._check_for_token()
        res = self.sess.post(
            url=APIMap.provider_list, 
            data=payload_to_bytes(self.provider_list_payload)
        )
        j = res.json()
        return self._extract_provider_info(j)

    @staticmethod
    def _extract_provider_schedule(res_json: dict) -> dict:
        """
        Extracts the JSON return from the stage 3 result and returns
        and ProviderInfo object.
        """
        fac_id = FacilityId(res_json['response']['prov_slots']['facility_id'])
        slots_by_day = res_json['response']['prov_slots']['appt_slots']
        times = {}
        next_slot = {}
        for day in slots_by_day:
            times[day['appt_date']] = [d['time'].rsplit(':', 1)[0] for d in day['appt_slots']]
            next_slot[day['appt_date']] = day.get('next_start_time')
            
        return {
            'facility': fac_id,
            'time_slots': times,
            'next_slot': next_slot
        }

    def _stage_3_get_provider_initial_schedule(self, infos: list[dict]) -> list[ProviderInfo]:
        """
        Third stage:
        - API call for the first few available slots for each provider's
          NPI at a facility.
        - Only the first 3 slots per day are returned.
        """
        self._check_for_token()
        out = []
        futures = {}
        ex = ThreadPoolExecutor(max_workers=self.workers)

        for info in infos:
            for fac_id in FacilityId:
                p = self.provider_details_payload.copy()
                p['provider_npi'] = info['npi']
                p['facility_id'] = fac_id.value
                f = ex.submit(
                    self.sess.post,
                    url=APIMap.provider_details,
                    data=payload_to_bytes(p)
                )
                futures[f] = (info, fac_id)
        
        for f in tqdm(as_completed(futures), desc='Providers', total=len(futures), leave=False):
            info, fac_id = futures[f]

            try:
                res = f.result()
                j = res.json()
                self._j = j
            except:
                self.errors.append((info, fac_id, f))

            schedule = self._extract_provider_schedule(j)
            out.append(ProviderInfo(**info, **schedule))
        return out

    @staticmethod
    def _extract_more_time_slots(res_json: dict) -> tuple[list[str], str|None]:
        """
        Extacts the additional time slot info from the JSON response in
        stage 4.

        returns:
            tuple: list[times], next_time_slot
        """
        time_slots = []
        appt_slots = (
            res_json['response']
                .get('appt_more_slots', {})
                .get('appt_slots', [])
        )
        more_next_time = (
            res_json['response']
                .get('appt_more_slots', {})
                .get('next_start_time')
        )

        for appt_dict in appt_slots:
            time_slots.append(appt_dict['time'].rsplit(':', 1)[0])
            
        return time_slots, more_next_time

    def _fill_more_time_slots(self, prov_info: ProviderInfo):
        """
        Repeated API calls to get the next few time slots until no
        more are returned.
        """
        self._check_for_token()
        p = self.provider_more_times_payload.copy()
        p['npi'] = prov_info.npi
        p['facility_id'] = prov_info.facility.value
        queue = []

        for date, next_time in prov_info.next_slot.items():
            if not next_time:
                continue
            queue.append((date, next_time))
            
        while queue:
            date, next_time = queue.pop(0)
            p['appt_date'] = date
            p['start_time'] = next_time

            res = self.sess.post(
                url=APIMap.provider_slots,
                data=payload_to_bytes(p)
            )
            res_json = res.json()
            time_slots, more_next_time = self._extract_more_time_slots(res_json)
            prov_info.time_slots[date].extend(time_slots)
            prov_info.next_slot[date] = more_next_time
            if more_next_time:
                queue.append((date, more_next_time))
        return prov_info

    def _stage_4_fill_more_times(self, prov_info_list: list[ProviderInfo]):
        """
        Fourth stage:
        - Multithreaded filling of "more" timeslots.
        """
        self._check_for_token()
        futures = {}
        out = []
        ex = ThreadPoolExecutor(max_workers=self.workers)
        
        for prov_info in prov_info_list:
            f = ex.submit(
                self._fill_more_time_slots,
                prov_info
            )
            futures[f] = prov_info
        
        for f in tqdm(as_completed(futures), desc='Providers', total=len(futures), leave=False):
            prov_info = futures[f]
            try:
                prov_info_filled = f.result()
            except:
                self.errors.append(f)
            out.append(prov_info_filled)

        return out


def provider_info_to_table_dicts(prov_info_list: list[ProviderInfo]):
    """
    Pivots a list of filled ProviderInfo objects to a nested dictionary.
    {
        date1: {time01: [name, name, name], time02: [name, name]},
        date2: {time01: ...}
    }
    """
    out_dicts = {
        FacilityId.GlebeRoadArlington: defaultdict(list),
        FacilityId.WalkerLaneSpringfield: defaultdict(list),
    }
    for prov in prov_info_list:
        out = out_dicts[prov.facility]
        for date, times in prov.time_slots.items():
            for time in times:
                h, m = map(int, time.split(':'))
                # coerce minutes to 10-minute intervals.
                m_slot = 10 * (m // 10)
                key = f'{date}T{h:0>2}:{m_slot:0>2}'
                value = f'[{time}] {prov.name} ({prov.degree})'
                out[key].append(value)
    return {k.name: dict(v) for k, v in out_dicts.items()}


APPT_TEMPLATE = """
<!DOCTYPE html>
<html lang="us-en">
<head>
  <title>PAA Time Calendar</title>
  <style>
    body {
      background-color: #2b1d1d;
      color: #ceae77;
    }
    table {
      border-collapse: collapse;
      font-size: .9em;
    }
    th, td {
      border: 1px solid #222;
      padding: 3px;
      text-align: center;
      width: 22em;
    }
    .time-slot {
        width: 5em;
    }
    th {
      background-color: #423F3E ;
    }
    td {
      vertical-align: text-top;
      background-color: #412c2c;
    }
    tr:nth-child(even) td {
      /* Alternate row background color */
      background-color: #3d2626;
    }
    .appt-cnt[data-value="0"] {
      color: transparent;
    } 
    .appt-cnt:not([data-value="0"]) {
      cursor: pointer;
    }
    .container {
      justify-content: center;
      margin-left: 50px;
      width: 95%;
    }
  </style>
</head>
<body>
<div class="container">

<h1>Glebe Road - Arlington</h1>
<table>
  <tr>
    <th class="time-slot">Time</th>
    {%- for date in dates %}
    <th>{{ date }}</th>
    {%- endfor %}
  </tr>
  
  {%- for time in times %}
    <tr>
    <td class="time-slot">{{ time }}</td>
    {%- for date in dates %}
      <td class="appt-cnt" data-value="{{ data['GlebeRoadArlington'].get(date + 'T' + time, '') | length }}">
        {{ data['GlebeRoadArlington'].get(date + 'T' + time, '') | length }}
        <div style="display: none; text-align: left;">
          <ul>
            {%- for provider in data['GlebeRoadArlington'].get(date + 'T' + time, []) | sort %}
            <li>{{ provider }}</li>
            {%- endfor %}
          </ul>
        </div>
      </td>
    {% endfor %}
    </tr>
  {% endfor %}
</table>

<br>
<h1>Walker Lane - Springfield</h1>
<table>
  <tr>
    <th class="time-slot">Time</th>
    {%- for date in dates %}
    <th>{{ date }}</th>
    {%- endfor %}
  </tr>
  
  {%- for time in times %}
    <tr>
    <td class="time-slot">{{ time }}</td>
    {%- for date in dates %}
      <td class="appt-cnt" data-value="{{ data['WalkerLaneSpringfield'].get(date + 'T' + time, '') | length }}">
        {{ data['WalkerLaneSpringfield'].get(date + 'T' + time, '') | length }}
        <div style="display: none; text-align: left;">
          <ul>
            {%- for provider in data['WalkerLaneSpringfield'].get(date + 'T' + time, []) %}
            <li>{{ provider }}</li>
            {%- endfor %}
          </ul>
        </div>
      </td>
    {% endfor %}
    </tr>
  {% endfor %}

</div>
</table>
<script>
  const trs = document.querySelectorAll('tr');
  trs.forEach(tr => {
    if (tr.querySelector('li')) {
      tr.addEventListener('click', () => {
        var divs = tr.querySelectorAll('div');
        divs.forEach(div => {
          div.style.display = div.style.display === 'none' ? 'block' : 'none';
        })
      });
    }
  });
</script>
</body>
</html>
"""


def render_html(dates: list[str], table_dicts: dict):
    """
    Renders a simple template to show the currently available 
    time slots.
    """
    start = dt.datetime(2020, 1, 1, 6, 0, 0)
    times = [
        (start + dt.timedelta(minutes=10 * i)).time().strftime('%H:%M')
        for i in range(85)
    ]
    h = Template(APPT_TEMPLATE).render(
        dates = dates,
        times = times,
        data = table_dicts
    )
    return h


if __name__ == '__main__':
    import webbrowser
    from pathlib import Path

    psn = PAAScheduleNabber()
    info_list = psn()
    table_dicts = provider_info_to_table_dicts(info_list)
    h = render_html([(dt.date.today() + dt.timedelta(days=i)).isoformat() for i in range(5)], table_dicts)
    
    path = Path('~/PAA-schedule.html').expanduser()
    with open(path, 'w') as fp:
        fp.write(h)
    print('--- Writing schedule to the HTML file: ---')
    print('      ', path)
    print('--- Opening web browser to view schedule ---')
    webbrowser.open(f'file:///{path.as_posix()}')

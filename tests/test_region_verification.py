import json

import httpx
import pytest

from scripts.verify_moscow_region import verify


def test_geo_verification_matches_config_and_excludes_session_fields():
    def handler(request):
        assert request.url.params['latitude'] == '55.7539'
        assert request.url.params['longitude'] == '37.6208'
        return httpx.Response(200, json={'xinfo': 'appType=1&curr=rub&dest=1259570991',
                                         'ip': 'DO-NOT-LOG', 'userDataSign': 'PRIVATE-SIGNATURE'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verify(client, 1259570991)
        assert result['status'] == 'PASS'
        assert result['moscow_destination'] == 1259570991
        assert verify(client, 999)['status'] == 'MISMATCH'
    encoded = json.dumps(result)
    assert 'DO-NOT-LOG' not in encoded and 'PRIVATE-SIGNATURE' not in encoded
    assert 'ip' not in result and 'userDataSign' not in result


@pytest.mark.parametrize('payload', [{}, {'xinfo': 4}, {'xinfo': 'dest=1&dest=2'}, {'xinfo': 'dest=broken'}])
def test_unknown_geo_response_is_not_accepted(payload):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
        with pytest.raises(ValueError):
            verify(client, 1259570991)
